"""Offline, non-installing checks for the independent Linux release controller."""
from __future__ import annotations

from pathlib import Path
import subprocess
import tempfile
import unittest
from dataclasses import dataclass
from unittest.mock import patch
import os

from linux_agent.service_plan import ReleasePair
from linux_agent.release_controller import (
    DisposableUserServiceController, ServiceSwitchError, verify_clean_exact_source,
)


def git(*args: str, cwd: Path | None = None) -> str:
    return subprocess.run(["git", *(("-C", str(cwd)) if cwd else ()), *args],
                          check=True, capture_output=True, text=True,
                          timeout=20).stdout.strip()


class ReleaseControllerSourceTests(unittest.TestCase):
    def test_release_requires_exact_clean_git_checkout_not_just_sha_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            origin = root / "seed"
            origin.mkdir()
            git("init", str(origin))
            (origin / "payload").write_text("baseline\n")
            git("add", "payload", cwd=origin)
            git("-c", "user.name=Fixture", "-c", "user.email=fixture@local.invalid",
                "commit", "-m", "baseline", cwd=origin)
            sha = git("rev-parse", "HEAD", cwd=origin)
            release = root / sha
            git("clone", "--quiet", str(origin), str(release))
            verify_clean_exact_source(release, sha)
            (release / "intruder.py").write_text("untracked\n")
            with self.assertRaisesRegex(ServiceSwitchError, "modified or untracked"):
                verify_clean_exact_source(release, sha)
            (release / "intruder.py").unlink()
            with self.assertRaisesRegex(ServiceSwitchError, "root/sha mismatch"):
                verify_clean_exact_source(release, "f" * 40)
            (release / "payload").write_text("modified\n")
            with self.assertRaisesRegex(ServiceSwitchError, "modified or untracked"):
                verify_clean_exact_source(release, sha)


    def test_lab_controller_rejects_actual_host_identity_even_with_sha_roots(self):
        with tempfile.TemporaryDirectory(prefix="linux-agent-release-") as tmp:
            root = Path(tmp)
            sha1, sha2 = "a" * 40, "b" * 40
            for sha in (sha1, sha2):
                pkg = root / "releases" / sha / "platforms/linux/local-agent/linux_agent"
                pkg.mkdir(parents=True)
                (pkg / "__main__.py").write_text("# fixture")
            python = root / "python"
            python.write_text("binary placeholder")
            config = root / "config.json"
            config.write_text('{"host_id":"debian-n5105","repository":"git@github.com:example-owner/local-agent.git","branch":"agent-control"}')
            config.chmod(0o600)
            state = root / "private-state"
            state.mkdir(mode=0o700)
            runtime = root / "runtime"
            runtime.mkdir()
            pair = ReleasePair(root / "releases" / sha1, sha1,
                               root / "releases" / sha2, sha2, python, config, state)
            with patch.dict(os.environ, {"XDG_RUNTIME_DIR": str(runtime)}):
                ctrl = DisposableUserServiceController(pair, "local-agent-linux-lab-" + "a" * 16 + ".service")
                with self.assertRaisesRegex(ServiceSwitchError, "isolated offline Git and identity"):
                    ctrl._guard()

    def test_production_unit_name_is_not_allowed_by_lab_controller(self):
        with self.assertRaisesRegex(ValueError, "only unique lab"):
            DisposableUserServiceController(None, "local-agent-linux.service")



@dataclass
class _OfflinePair:
    candidate: Path
    candidate_sha: str
    rollback: Path
    rollback_sha: str
    state: Path

    def validate(self):
        pass

    def unit_for(self, release, sha):
        if (release, sha) not in (
            (self.candidate, self.candidate_sha),
            (self.rollback, self.rollback_sha),
        ):
            raise ValueError("unrecognized release")
        return f"fixture-unit:{sha}\n"


class ReleaseJournalTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="la-journal-offline-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        state = self.root / "private"
        state.mkdir(mode=0o700)
        self.pair = _OfflinePair(
            self.root / ("a" * 40), "a" * 40,
            self.root / ("b" * 40), "b" * 40, state,
        )
        self.run_dir = self.root / "runtime"
        (self.run_dir / "systemd/user").mkdir(parents=True)
        self.env = patch.dict(os.environ, {"XDG_RUNTIME_DIR": str(self.run_dir)})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.name = "local-agent-linux-lab-" + "1" * 16 + ".service"
        self.ctrl = DisposableUserServiceController(self.pair, self.name)
        self.active = None
        self.pid = 12345
        self.calls = []

        def fake_write(contents):
            self.ctrl.unit_file.write_text(contents)
            self.calls.append(("write", contents))

        def fake_stop():
            self.calls.append(("stop", self.active))
            self.active = None

        def fake_start(release):
            self.calls.append(("start", release))
            self.active = release
            self.pid += 1
            return self.pid

        def fake_check(release):
            if self.active != release:
                raise ServiceSwitchError("service is not running expected release")
            return self.pid

        self.guard = patch.object(
            DisposableUserServiceController, "_guard", return_value=None
        )
        self.guard.start()
        self.addCleanup(self.guard.stop)
        self.write = patch.object(self.ctrl, "_write", side_effect=fake_write)
        self.write.start()
        self.addCleanup(self.write.stop)
        self.stop = patch.object(self.ctrl, "_stop", side_effect=fake_stop)
        self.stop.start()
        self.addCleanup(self.stop.stop)
        self.start = patch.object(self.ctrl, "_start", side_effect=fake_start)
        self.start.start()
        self.addCleanup(self.start.stop)
        self.check = patch.object(self.ctrl, "_check_active", side_effect=fake_check)
        self.check.start()
        self.addCleanup(self.check.stop)

    def test_journal_survives_unverified_interruption_and_restores_baseline(self):
        self.ctrl.stage_baseline()
        self.assertEqual(self.ctrl._journal()["phase"], "baseline_active")
        self.assertEqual(self.active, self.pair.rollback)
        # Model SIGKILL BETWEEN stopping the old service and completing
        # the new one. Recovery must use a fresh external controller.
        self.ctrl._record("candidate_requested")
        self.ctrl._stop()
        self.ctrl._record("candidate_unverified")
        self.ctrl._write(self.pair.unit_for(self.pair.candidate, self.pair.candidate_sha))
        self.assertEqual(self.ctrl._journal()["phase"], "candidate_unverified")
        recovered = DisposableUserServiceController(self.pair, self.name)
        with patch.object(recovered, "_write", side_effect=self.ctrl._write), \
             patch.object(recovered, "_stop", side_effect=self.ctrl._stop), \
             patch.object(recovered, "_start", side_effect=self.ctrl._start), \
             patch.object(recovered, "_check_active", side_effect=self.ctrl._check_active):
            self.assertGreater(recovered.recover_unverified(), 1)
            self.assertEqual(recovered._journal()["phase"], "baseline_active")
        self.assertEqual(self.active, self.pair.rollback)
        self.assertEqual(self.ctrl.unit_file.read_text(),
                         self.pair.unit_for(self.pair.rollback, self.pair.rollback_sha))

    def test_candidate_success_then_explicit_rollback(self):
        self.ctrl.stage_baseline()
        self.ctrl.switch_to_candidate()
        self.assertEqual(self.ctrl._journal()["phase"], "candidate_active")
        self.assertEqual(self.active, self.pair.candidate)
        self.assertIsNone(self.ctrl.recover_unverified())  # don't undo accepted
        self.ctrl.rollback_candidate()
        self.assertEqual(self.ctrl._journal()["phase"], "baseline_active")
        self.assertEqual(self.active, self.pair.rollback)

    def test_corrupt_or_cross_unit_journal_fails_closed(self):
        self.ctrl.stage_baseline()
        self.ctrl.transaction_path.write_text('{"phase":"baseline_active"}')
        with self.assertRaisesRegex(ServiceSwitchError, "identity/state mismatch"):
            self.ctrl.switch_to_candidate()
        self.assertEqual(self.active, self.pair.rollback)
        self.assertEqual(self.calls.count(("stop", self.pair.rollback)), 0)


    def test_start_of_fresh_unit_skips_reset_failed(self):
        # Debian systemd returns rc=1 for reset-failed on a never-loaded unit.
        self.active = self.pair.rollback
        with patch.object(self.ctrl, "_show", return_value="inactive"), \
             patch("linux_agent.release_controller._run", return_value="") as run:
            self.assertGreater(
                DisposableUserServiceController._start(self.ctrl, self.pair.rollback), 1
            )
        run.assert_called_once_with("systemctl", "--user", "start", self.name)

    def test_start_of_failed_unit_resets_failure_before_start(self):
        self.active = self.pair.rollback
        with patch.object(self.ctrl, "_show", return_value="failed"), \
             patch("linux_agent.release_controller._run", return_value="") as run:
            self.assertGreater(
                DisposableUserServiceController._start(self.ctrl, self.pair.rollback), 1
            )
        self.assertEqual([item.args for item in run.call_args_list], [
            ("systemctl", "--user", "reset-failed", self.name),
            ("systemctl", "--user", "start", self.name),
        ])

    def test_candidate_activation_failure_restores_baseline_with_journal(self):
        self.ctrl.stage_baseline()
        original_start = self.ctrl._start

        def fail_candidate(release):
            pid = original_start(release)
            if release == self.pair.candidate:
                raise ServiceSwitchError("injected after candidate starts")
            return pid

        with patch.object(self.ctrl, "_start", side_effect=fail_candidate):
            with self.assertRaisesRegex(ServiceSwitchError, "rollback restored"):
                self.ctrl.switch_to_candidate()
        self.assertEqual(self.ctrl._journal()["phase"], "baseline_active")
        self.assertEqual(self.active, self.pair.rollback)

    def test_failed_rollback_retains_intent_for_external_recovery(self):
        self.ctrl.stage_baseline()
        self.ctrl.switch_to_candidate()
        with patch.object(self.ctrl, "_start", side_effect=ServiceSwitchError("injected start failure")):
            with self.assertRaisesRegex(ServiceSwitchError, "injected start failure"):
                self.ctrl.rollback_candidate()
        self.assertEqual(self.ctrl._journal()["phase"], "rollback_requested")
        self.assertGreater(self.ctrl.recover_unverified(), 1)
        self.assertEqual(self.ctrl._journal()["phase"], "baseline_active")
        self.assertEqual(self.active, self.pair.rollback)

    def test_unexpected_unit_never_overwritten_by_recovery(self):
        self.ctrl.stage_baseline()
        self.ctrl._record("candidate_unverified")
        self.ctrl.unit_file.write_text("unrelated service definition")
        with self.assertRaisesRegex(ServiceSwitchError, "unexpected unit"):
            self.ctrl.recover_unverified()
        self.assertEqual(self.ctrl.unit_file.read_text(), "unrelated service definition")
        self.assertEqual(self.ctrl._journal()["phase"], "candidate_unverified")

    def test_cleanup_keeps_terminal_journal_and_blocks_restart(self):
        self.ctrl.stage_baseline()
        with patch("linux_agent.release_controller._run", return_value=""):
            self.ctrl.cleanup_lab()
            self.ctrl.cleanup_lab()
        self.assertFalse(self.ctrl.unit_file.exists())
        self.assertEqual(self.ctrl._journal()["phase"], "cleaned")
        with self.assertRaisesRegex(ServiceSwitchError, "existing unit or journal"):
            self.ctrl.stage_baseline()

    def test_interrupted_start_can_be_recovered_from_baseline_starting(self):
        self.ctrl._record("baseline_starting")
        self.assertGreater(self.ctrl.recover_unverified(), 1)
        self.assertEqual(self.ctrl._journal()["phase"], "baseline_active")
        self.assertEqual(self.active, self.pair.rollback)

if __name__ == "__main__":
    unittest.main()
