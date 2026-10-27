from __future__ import annotations

import argparse
import contextlib
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from local_agent import entrypoint
from local_agent.daemon import service
from local_agent.daemon.installation import (
    installation_pending,
    installation_transaction,
    pending_path,
)
from local_agent.operator import local, remote
from local_agent.repository import worker as serial
from local_agent.repository.context import RepositoryContext
from local_agent.supervisor import worker as parallel
from tests.test_multirepo_integration import REPO_ROOT, configure_identity, git


class AdmissionRegressionTests(unittest.TestCase):
    def test_disable_control_stops_the_same_turn_on_both_workers(self) -> None:
        repository = RepositoryContext(
            "test", "owner/test", Path("/tmp/control"), Path("/tmp/work"), Path("/tmp/checkpoints")
        )
        for worker in (serial, parallel):
            with self.subTest(worker=worker.__name__), contextlib.ExitStack() as stack:
                stack.enter_context(mock.patch.object(serial, "bind_repository"))
                stack.enter_context(mock.patch.object(serial, "recover_claims_with_quarantine_layout"))
                stack.enter_context(mock.patch.object(serial, "validate_repository_checkouts"))
                stack.enter_context(mock.patch.object(serial, "sync_control_quietly"))
                ready = (
                    "repository_binding_ready" if worker is serial else "_repository_binding_ready"
                )
                stack.enter_context(mock.patch.object(worker, ready, return_value=True))
                stack.enter_context(mock.patch.object(service, "recover_stale_claims"))
                stack.enter_context(mock.patch.object(service, "recover_invalid_task_files"))
                stack.enter_context(
                    mock.patch.object(local, "is_disabled", side_effect=[False, True])
                )
                control = stack.enter_context(
                    mock.patch.object(serial, "handle_repository_control")
                )
                pending = stack.enter_context(mock.patch.object(service, "pending_tasks"))
                execute = stack.enter_context(mock.patch.object(service, "execute_task"))
                publish = stack.enter_context(
                    mock.patch.object(serial, "publish_repository_status")
                )
                self.assertFalse(worker.poll_repository_once(repository))
                control.assert_called_once_with(repository)
                pending.assert_not_called()
                execute.assert_not_called()
                self.assertEqual(publish.call_args.args[1], "disabled")

    def test_nonregular_and_inaccessible_disable_markers_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            marker = Path(tmp) / "disabled.json"
            with mock.patch.object(local, "DISABLED_PATH", marker):
                self.assertFalse(local.is_disabled())
                marker.mkdir()
                self.assertTrue(local.is_disabled())
                marker.rmdir()
                marker.symlink_to(Path(tmp) / "missing-target")
                self.assertTrue(local.is_disabled())
                marker.unlink()
                with mock.patch.object(Path, "lstat", side_effect=PermissionError("denied")):
                    self.assertTrue(local.is_disabled())

    def test_operator_module_executes_disable_status_and_enable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = dict(os.environ, HOME=tmp)
            for command, disabled in (
                ("disable", True),
                ("status", True),
                ("enable", False),
                ("status", False),
            ):
                result = subprocess.run(
                    [sys.executable, "-m", "local_agent.operator.local", command],
                    cwd=REPO_ROOT,
                    env=env,
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIs(json.loads(result.stdout)["disabled"], disabled)

    def test_malformed_wait_status_can_be_replaced(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "status.json"
            path.write_text("[]")
            with mock.patch.object(service, "LOCAL_STATUS_PATH", path):
                since, changed = parallel._waiting_status_context("pending", "machine")
            self.assertTrue(since)
            self.assertTrue(changed)


class InstalledUpdateIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.origin = self.root / "origin.git"
        self.seed = self.root / "seed"
        self.checkout = self.root / "installed"
        self.state = self.root / "state"
        git(["init", "--bare", str(self.origin)])
        git(["init", "-b", "main", str(self.seed)])
        configure_identity(self.seed)
        (self.seed / "scripts").mkdir()
        (self.seed / "local_agent").mkdir()
        (self.seed / "local_agent/__init__.py").write_text("")
        (self.seed / "local_agent/version.py").write_text("VERSION = 'old'\n")
        (self.seed / "scripts/verify.py").write_text((REPO_ROOT / "scripts/verify.py").read_text())
        (self.seed / ".gitignore").write_text("__pycache__/\n*.pyc\n")
        (self.seed / "test_version.py").write_text(
            "import unittest\nfrom local_agent.version import VERSION\n"
            "class VersionTest(unittest.TestCase):\n"
            " def test_installed_version(self):\n  self.assertIsInstance(VERSION, str)\n"
        )
        git(
            [
                "add",
                ".gitignore",
                "test_version.py",
                "scripts/verify.py",
                "local_agent/__init__.py",
                "local_agent/version.py",
            ],
            cwd=self.seed,
        )
        git(["commit", "-m", "Initial installed source"], cwd=self.seed)
        git(["remote", "add", "origin", str(self.origin)], cwd=self.seed)
        git(["push", "origin", "main"], cwd=self.seed)
        git(["clone", "--branch", "main", str(self.origin), str(self.checkout)])
        self.original = git(["rev-parse", "HEAD"], cwd=self.checkout).strip()
        self.enterContext(mock.patch.object(service, "SELF_REPO", self.checkout))
        self.enterContext(mock.patch.object(service, "STATE_DIR", self.state))
        self.enterContext(mock.patch.object(local, "STATE_DIR", self.state))
        self.enterContext(mock.patch.object(local, "DISABLED_PATH", self.state / "disabled.json"))
        self.enterContext(
            mock.patch.object(service, "REJECTED_UPDATE_PATH", self.state / "rejected.json")
        )
        self.enterContext(mock.patch.object(service, "_last_self_update_check", 0.0))

    def advance(self, source: str) -> str:
        (self.seed / "local_agent/version.py").write_text(source)
        git(["add", "local_agent/version.py"], cwd=self.seed)
        git(["commit", "-m", "Candidate installed source"], cwd=self.seed)
        git(["push", "origin", "main"], cwd=self.seed)
        return git(["rev-parse", "HEAD"], cwd=self.seed).strip()

    def test_invalid_packaged_update_rolls_back_without_restart_or_repeat(self) -> None:
        target = self.advance("this is invalid Python!\n")
        with mock.patch.object(service, "restart_self") as restart:
            self.assertFalse(service.maybe_self_update(force=True))
            self.assertEqual(git(["rev-parse", "HEAD"], cwd=self.checkout).strip(), self.original)
            rejection = json.loads(service.REJECTED_UPDATE_PATH.read_text())
            self.assertEqual(rejection["sha"], target)
            self.assertEqual(rejection["reason"], "validation_failed")
            with mock.patch.object(service, "_validate_installed_update") as validate:
                self.assertFalse(service.maybe_self_update(force=True))
                validate.assert_not_called()
            restart.assert_not_called()
        self.assertFalse(installation_pending(self.state))

    def test_validation_exception_rolls_back(self) -> None:
        self.advance("VERSION = 'new'\n")
        with (
            mock.patch.object(
                service, "_validate_installed_update", side_effect=OSError("probe failed")
            ),
            mock.patch.object(service, "restart_self") as restart,
        ):
            self.assertFalse(service.maybe_self_update(force=True))
        restart.assert_not_called()
        self.assertEqual(git(["rev-parse", "HEAD"], cwd=self.checkout).strip(), self.original)
        self.assertFalse(installation_pending(self.state))

    def test_update_installs_the_inspected_commit_when_remote_advances(self) -> None:
        target = self.advance("VERSION = 'inspected'\n")
        original_git = service._git

        def advance_during_merge(args, **kwargs):
            if args[1] == "merge":
                self.assertEqual(args[-1], target)
                self.advance("VERSION = 'later'\n")
            return original_git(args, **kwargs)

        with (
            mock.patch.object(service, "_git", side_effect=advance_during_merge),
            mock.patch.object(service, "restart_self"),
        ):
            self.assertTrue(service.maybe_self_update(force=True))
        self.assertEqual(git(["rev-parse", "HEAD"], cwd=self.checkout).strip(), target)

    def test_killed_update_is_revalidated_and_recovers_on_guard_restart(self) -> None:
        target = self.advance("VERSION = 'unvalidated'\n")
        ready = self.root / "validation-started"
        code = (
            "from pathlib import Path\nimport time\n"
            "from local_agent.daemon import service\n"
            f"service.SELF_REPO = Path({str(self.checkout)!r})\n"
            f"service.STATE_DIR = Path({str(self.state)!r})\n"
            "service.REJECTED_UPDATE_PATH = service.STATE_DIR / 'rejected.json'\n"
            "def validate():\n"
            f" Path({str(ready)!r}).touch()\n"
            " time.sleep(30)\n return True, ''\n"
            "service._validate_installed_update = validate\n"
            "service.maybe_self_update(force=True)\n"
        )
        proc = subprocess.Popen([sys.executable, "-c", code], cwd=REPO_ROOT)
        try:
            deadline = time.monotonic() + 10
            while not ready.exists() and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertTrue(ready.exists())
            proc.kill()
            proc.wait(timeout=5)
        finally:
            if proc.poll() is None:
                proc.kill()
            proc.wait(timeout=5)

        self.assertEqual(
            json.loads(pending_path(self.state).read_text()),
            {
                "original_revision": self.original,
                "candidate_revision": target,
            },
        )
        with self.assertRaisesRegex(RuntimeError, "operator recovery"):
            local.enable_agent()

        local.disable_agent(reason="interrupted_self_update")
        with mock.patch.object(
            service, "_validate_installed_update", return_value=(True, "")
        ) as validate:
            recovered, detail = entrypoint.recover_interrupted_installation()
        self.assertTrue(recovered)
        self.assertEqual(detail, "validated")
        validate.assert_called_once_with()
        self.assertFalse(installation_pending(self.state))
        self.assertFalse(local.is_disabled())
        self.assertEqual(git(["rev-parse", "HEAD"], cwd=self.checkout).strip(), target)

    def test_interrupted_update_recovery_never_clears_operator_disable(self) -> None:
        target = self.advance("VERSION = 'candidate'\n")
        from local_agent.daemon.installation import begin_installation

        begin_installation(self.state, self.original, target)
        git(["fetch", "origin", "main"], cwd=self.checkout)
        git(["merge", "--ff-only", "--quiet", target], cwd=self.checkout)
        local.disable_agent(reason="operator_cli")

        with mock.patch.object(
            service, "_validate_installed_update", return_value=(True, "")
        ):
            recovered, detail = entrypoint.recover_interrupted_installation()

        self.assertTrue(recovered)
        self.assertEqual(detail, "validated")
        self.assertFalse(installation_pending(self.state))
        self.assertTrue(local.is_disabled())
        self.assertEqual(local.disabled_state()["reason"], "operator_cli")

    def test_valid_update_holds_installation_lock_until_validation_finishes(self) -> None:
        target = self.advance("VERSION = 'new'\n")
        validate = service._validate_installed_update

        def locked_validation():
            with installation_transaction(self.state) as acquired:
                self.assertFalse(acquired)
            return validate()

        with (
            mock.patch.object(service, "_validate_installed_update", side_effect=locked_validation),
            mock.patch.object(service, "restart_self") as restart,
        ):
            self.assertTrue(service.maybe_self_update(force=True))
        restart.assert_called_once_with("self_update")
        self.assertEqual(git(["rev-parse", "HEAD"], cwd=self.checkout).strip(), target)
        with installation_transaction(self.state) as acquired:
            self.assertTrue(acquired)

    def test_guard_defers_reexec_while_installed_candidate_is_unvalidated(self) -> None:
        args = argparse.Namespace(registry=None, max_workers=2)
        old_stop = entrypoint._stop_requested
        self.addCleanup(setattr, entrypoint, "_stop_requested", old_stop)
        entrypoint._stop_requested = False

        def finish(_seconds):
            entrypoint._stop_requested = True

        with installation_transaction(self.state) as acquired:
            self.assertTrue(acquired)
            with (
                mock.patch.object(entrypoint, "parse_args", return_value=args),
                mock.patch.object(entrypoint, "install_signal_handlers"),
                mock.patch.object(service, "self_revision", side_effect=["old", "candidate"]),
                mock.patch.object(remote, "poll_remote_operator"),
                mock.patch.object(local, "is_disabled", return_value=True),
                mock.patch.object(entrypoint, "publish_guard_status"),
                mock.patch.object(entrypoint.time, "sleep", side_effect=finish),
                mock.patch.object(entrypoint.os, "execv") as execute,
            ):
                self.assertEqual(entrypoint.main(), 0)
                execute.assert_not_called()

    def test_installation_lock_survives_contender_and_releases_on_process_death(self) -> None:
        ready = self.root / "ready"
        code = (
            "from pathlib import Path; import time; "
            "from local_agent.daemon.installation import installation_transaction; "
            f"lock=installation_transaction(Path({str(self.state)!r})); "
            "assert lock.__enter__(); "
            f"Path({str(ready)!r}).touch(); time.sleep(30)"
        )
        proc = subprocess.Popen([sys.executable, "-c", code], cwd=REPO_ROOT)
        try:
            deadline = time.monotonic() + 10
            while not ready.exists() and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertTrue(ready.exists())
            with installation_transaction(self.state) as acquired:
                self.assertFalse(acquired)
            proc.send_signal(signal.SIGKILL)
            proc.wait(timeout=5)
            with installation_transaction(self.state) as acquired:
                self.assertTrue(acquired)
        finally:
            if proc.poll() is None:
                proc.kill()
            proc.wait(timeout=5)

    def test_operator_payload_ignores_fetch_head_after_exact_ref_validation(self) -> None:
        git(["checkout", "-b", "operator-control"], cwd=self.seed)
        state_path = self.seed / remote.REMOTE_STATE_PATH
        state_path.parent.mkdir(parents=True)
        state_path.write_text(
            json.dumps({"version": 1, "desired_state": "disabled", "request_id": "stop"})
        )
        git(["add", remote.REMOTE_STATE_PATH], cwd=self.seed)
        git(["commit", "-m", "Operator disabled state"], cwd=self.seed)
        git(["push", "origin", "operator-control"], cwd=self.seed)
        expected = git(["rev-parse", "HEAD"], cwd=self.seed).strip()
        original_git = remote._git

        def interleaved_git(args, self_repo, **kwargs):
            result = original_git(args, self_repo, **kwargs)
            if args[0] == "rev-parse":
                git(["fetch", "origin", "main"], cwd=self.checkout)
            return result

        with mock.patch.object(remote, "_git", side_effect=interleaved_git):
            payload = remote._load_remote_payload(self.checkout, expected)
        self.assertEqual(payload["desired_state"], "disabled")
        self.assertEqual(git(["rev-parse", "FETCH_HEAD"], cwd=self.checkout).strip(), self.original)


if __name__ == "__main__":
    unittest.main()
