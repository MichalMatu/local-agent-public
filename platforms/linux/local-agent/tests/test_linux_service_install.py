"""Offline source acceptance of non-activating persistent user-service staging."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from linux_agent.release_controller import ServiceSwitchError
from linux_agent.service_install import (
    UNIT_NAME, PersistentUnitStage, _run_systemctl,
)
from linux_agent.service_plan import ReleasePair


def git(*args: str) -> str:
    return subprocess.run(
        ["git", *args], check=True, capture_output=True, text=True, timeout=25
    ).stdout.strip()


class InstalledStageOfflineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="linux-install-offline-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        seed = self.root / "seed"
        git("init", "-q", str(seed))
        pkg = seed / "platforms/linux/local-agent/linux_agent"
        pkg.mkdir(parents=True)
        (pkg / "__main__.py").write_text("print('baseline')\n")
        git("-C", str(seed), "add", ".")
        git("-C", str(seed), "-c", "user.name=Fixture", "-c",
            "user.email=fixture@local.invalid", "commit", "-qm", "baseline")
        baseline = git("-C", str(seed), "rev-parse", "HEAD")
        (pkg / "__main__.py").write_text("print('candidate')\n")
        git("-C", str(seed), "add", ".")
        git("-C", str(seed), "-c", "user.name=Fixture", "-c",
            "user.email=fixture@local.invalid", "commit", "-qm", "candidate")
        candidate = git("-C", str(seed), "rev-parse", "HEAD")
        releases = self.root / "releases"
        releases.mkdir()
        for sha in (baseline, candidate):
            git("clone", "-q", "--local", "--no-hardlinks", str(seed),
                str(releases / sha))
            git("-C", str(releases / sha), "checkout", "-q", "--detach", sha)
        self.state = self.root / "private-state"
        self.state.mkdir(mode=0o700)
        (self.state / "EMERGENCY_STOP").write_text("disabled\n")
        self.config = self.root / "config.json"
        self.config.write_text(json.dumps({
            "version": 1, "host_id": "debian-fixture",
            "installation_id": "123e4567-e89b-12d3-a456-426614174000",
            "repository": str(self.root / "dummy-remote.git"),
            "branch": "agent-control", "enabled": True,
            "host_intake": {"enabled": True,
                            "authorization": "github_writers"},
        }))
        self.config.chmod(0o600)
        pair = ReleasePair(
            candidate=releases / candidate, candidate_sha=candidate,
            rollback=releases / baseline, rollback_sha=baseline,
            python=Path(sys.executable).resolve(),
            config=self.config, state=self.state,
        )
        self.stage = PersistentUnitStage(pair)
        # Inject only an owner-private temporary home, never the actual
        # ~/.config/systemd/user or Debian pilot paths.
        self.stage.home = self.root / "home"
        self.stage.home.mkdir(mode=0o700)
        self.stage.unit_dir = self.stage.home / ".config/systemd/user"
        self.stage.unit_file = self.stage.unit_dir / UNIT_NAME
        self.events = []

    def fake_systemd(self, *args: str, **kwargs: object) -> str:
        self.events.append(args)
        if args[0] == "show":
            field = args[2].removeprefix("--property=")
            exists = self.stage.unit_file.exists()
            return {
                "LoadState": "loaded" if exists else "not-found",
                "FragmentPath": str(self.stage.unit_file) if exists else "",
                "ActiveState": "inactive",
            }[field]
        if args[0] == "is-active":
            return "inactive"
        if args[0] == "is-enabled":
            return "disabled"
        return ""

    def failing_reload(self, *args: str, **kwargs: object) -> str:
        if args[0] == "show":
            return "not-found"
        raise ServiceSwitchError("reload interrupted")

    def test_preview_has_no_fs_or_systemd_mutations(self):
        with patch("linux_agent.service_install._run_systemctl",
                   side_effect=AssertionError("preview touched systemd")):
            data = self.stage.preview()
        self.assertEqual(data["status"], "ready_to_stage_not_installed")
        self.assertEqual(data["activation"], "not_authorized_or_performed")
        self.assertFalse(self.stage.unit_dir.exists())
        self.assertFalse(self.stage.journal.exists())
        self.assertFalse(self.stage.lock.exists())

    def test_stage_and_withdraw_create_only_exact_inactive_unit(self):
        with patch("linux_agent.service_install._run_systemctl",
                   side_effect=self.fake_systemd):
            prepared = self.stage.stage()
            self.assertEqual(prepared["status"], "staged_disabled")
            unit = self.stage.unit_file.read_text()
            self.assertIn(self.stage.pair.candidate_sha, unit)
            self.assertIn("KillMode=control-group", unit)
            self.assertEqual(self.stage.unit_file.stat().st_mode & 0o777, 0o600)
            self.assertEqual(self.stage.unit_dir.stat().st_mode & 0o777, 0o700)
            self.assertEqual(
                json.loads(self.stage.journal.read_text())["unit_sha256"],
                hashlib.sha256(unit.encode()).hexdigest(),
            )
            self.assertEqual(self.stage.withdraw()["status"], "withdrawn")
        self.assertFalse(self.stage.unit_file.exists())
        self.assertEqual(json.loads(self.stage.journal.read_text())["phase"],
                         "withdrawn")
        self.assertEqual(self.events, [
            ("show", UNIT_NAME, "--property=LoadState", "--value"),
            ("daemon-reload",),
            ("show", UNIT_NAME, "--property=LoadState", "--value"),
            ("show", UNIT_NAME, "--property=FragmentPath", "--value"),
            ("is-active", UNIT_NAME), ("is-enabled", UNIT_NAME),
            ("is-active", UNIT_NAME), ("is-enabled", UNIT_NAME),
            ("daemon-reload",),
            ("show", UNIT_NAME, "--property=LoadState", "--value"),
            ("show", UNIT_NAME, "--property=ActiveState", "--value"),
            ("show", UNIT_NAME, "--property=FragmentPath", "--value"),
        ])
        with self.assertRaisesRegex(ServiceSwitchError, "not in safely staged"):
            self.stage.withdraw()

    def test_preexisting_unit_will_never_be_overwritten(self):
        self.stage.unit_dir.mkdir(parents=True)
        self.stage.unit_file.write_text("Existing unrelated user service")
        with patch("linux_agent.service_install._run_systemctl",
                   side_effect=AssertionError("must not reload")):
            with self.assertRaisesRegex(ServiceSwitchError, "existing unit"):
                self.stage.stage()
        self.assertEqual(self.stage.unit_file.read_text(),
                         "Existing unrelated user service")
        self.assertFalse(self.stage.journal.exists())

    def test_changed_unit_cannot_be_withdrawn(self):
        with patch("linux_agent.service_install._run_systemctl",
                   side_effect=self.fake_systemd):
            self.stage.stage()
            self.stage.unit_file.write_text("changed outside stage")
            with self.assertRaisesRegex(ServiceSwitchError, "contents changed"):
                self.stage.withdraw()
        self.assertEqual(self.stage.unit_file.read_text(),
                         "changed outside stage")
        self.assertEqual(json.loads(self.stage.journal.read_text())["phase"],
                         "staged")

    def test_enabled_unit_refuses_withdrawal(self):
        with patch("linux_agent.service_install._run_systemctl",
                   side_effect=self.fake_systemd):
            self.stage.stage()

        def enabled(*args, **kwargs):
            if args[0] == "is-active":
                return "inactive"
            if args[0] == "is-enabled":
                return "enabled"
            raise AssertionError("must not mutate")

        with patch("linux_agent.service_install._run_systemctl",
                   side_effect=enabled):
            with self.assertRaisesRegex(ServiceSwitchError, "was enabled"):
                self.stage.withdraw()
        self.assertTrue(self.stage.unit_file.exists())

    def test_emergency_stop_is_required(self):
        (self.state / "EMERGENCY_STOP").unlink()
        with self.assertRaisesRegex(ServiceSwitchError, "EMERGENCY_STOP"):
            self.stage.preview()
        self.assertFalse(self.stage.journal.exists())
        self.assertFalse(self.stage.unit_file.exists())

    def test_altered_source_cannot_be_staged(self):
        modified = self.stage.pair.candidate / "modified-file.txt"
        modified.write_text("not clean")
        with self.assertRaisesRegex(ServiceSwitchError, "modified or untracked"):
            self.stage.preview()
        self.assertFalse(self.stage.unit_file.exists())

    def test_bad_host_configuration_cannot_be_staged(self):
        value = json.loads(self.config.read_text())
        value["branch"] = "not-agent-control"
        self.config.write_text(json.dumps(value))
        with self.assertRaisesRegex(ValueError, "branch mismatch"):
            self.stage.preview()
        self.assertFalse(self.stage.unit_file.exists())

    def test_preexisting_manager_unit_name_blocks_stage(self):
        def already_loaded(*args, **kwargs):
            if args[0] == "show":
                return "loaded"
            raise AssertionError("must not mutate systemd")
        with patch("linux_agent.service_install._run_systemctl",
                   side_effect=already_loaded):
            with self.assertRaisesRegex(ServiceSwitchError,
                                        "systemd already knows unit"):
                self.stage.stage()
        self.assertFalse(self.stage.unit_file.exists())
        self.assertFalse(self.stage.journal.exists())

    def test_symlink_unit_path_refused_without_overwriting_target(self):
        outside = self.root / "unrelated-unit"
        outside.write_text("leave me alone")
        self.stage.unit_dir.mkdir(parents=True)
        self.stage.unit_file.symlink_to(outside)
        with self.assertRaisesRegex(ServiceSwitchError, "unsafe user unit"):
            self.stage.preview()
        self.assertEqual(outside.read_text(), "leave me alone")

    def test_unconfirmed_reload_keeps_durable_preparing_intent(self):
        with patch("linux_agent.service_install._run_systemctl",
                   side_effect=self.failing_reload):
            with self.assertRaisesRegex(ServiceSwitchError, "reload interrupted"):
                self.stage.stage()
        self.assertTrue(self.stage.unit_file.exists())
        self.assertEqual(json.loads(self.stage.journal.read_text())["phase"],
                         "preparing")
        with patch("linux_agent.service_install._run_systemctl",
                   side_effect=AssertionError("must not reload")):
            with self.assertRaisesRegex(ServiceSwitchError, "existing unit"):
                self.stage.stage()
        self.assertTrue(self.stage.unit_file.exists())

    def test_reconcile_interrupted_staging_never_starts_service(self):
        with patch("linux_agent.service_install._run_systemctl",
                   side_effect=self.failing_reload):
            with self.assertRaisesRegex(ServiceSwitchError, "reload interrupted"):
                self.stage.stage()
        self.assertEqual(json.loads(self.stage.journal.read_text())["phase"],
                         "preparing")
        with patch("linux_agent.service_install._run_systemctl",
                   side_effect=self.fake_systemd):
            out = self.stage.reconcile()
        self.assertEqual(out["status"], "staged_disabled_recovered")
        self.assertEqual(json.loads(self.stage.journal.read_text())["phase"],
                         "staged")
        self.assertEqual(self.events, [
            ("is-active", UNIT_NAME), ("is-enabled", UNIT_NAME),
            ("daemon-reload",),
            ("show", UNIT_NAME, "--property=LoadState", "--value"),
            ("show", UNIT_NAME, "--property=FragmentPath", "--value"),
            ("is-active", UNIT_NAME), ("is-enabled", UNIT_NAME),
        ])
        self.assertTrue(self.stage.unit_file.is_file())

    def test_reconcile_interrupted_withdrawal_of_absent_unit(self):
        with patch("linux_agent.service_install._run_systemctl",
                   side_effect=self.fake_systemd):
            self.stage.stage()
        original = self.stage.unit_file.read_text()
        self.stage._write_phase("withdrawing", original)
        self.stage.unit_file.unlink()
        self.events.clear()
        with patch("linux_agent.service_install._run_systemctl",
                   side_effect=self.fake_systemd):
            out = self.stage.reconcile()
        self.assertEqual(out["status"], "withdrawn_recovered")
        self.assertEqual(self.events, [
            ("daemon-reload",),
            ("show", UNIT_NAME, "--property=LoadState", "--value"),
            ("show", UNIT_NAME, "--property=ActiveState", "--value"),
            ("show", UNIT_NAME, "--property=FragmentPath", "--value"),
        ])
        self.assertEqual(json.loads(self.stage.journal.read_text())["phase"],
                         "withdrawn")
        self.assertFalse(self.stage.unit_file.exists())

    def test_interrupted_stage_with_replaced_bytes_refused(self):
        with patch("linux_agent.service_install._run_systemctl",
                   side_effect=self.failing_reload):
            with self.assertRaises(ServiceSwitchError):
                self.stage.stage()
        self.stage.unit_file.write_text("unexpected attacker content")
        with patch("linux_agent.service_install._run_systemctl",
                   side_effect=AssertionError("must not touch systemd")):
            with self.assertRaisesRegex(ServiceSwitchError, "contents changed"):
                self.stage.reconcile()
        self.assertEqual(json.loads(self.stage.journal.read_text())["phase"],
                         "preparing")
        self.assertEqual(self.stage.unit_file.read_text(),
                         "unexpected attacker content")

    def test_reconcile_refuses_running_unit(self):
        with patch("linux_agent.service_install._run_systemctl",
                   side_effect=self.failing_reload):
            with self.assertRaises(ServiceSwitchError):
                self.stage.stage()
        def active(*args, **kwargs):
            if args[0] == "is-active":
                return "active"
            raise AssertionError("must not progress")
        with patch("linux_agent.service_install._run_systemctl",
                   side_effect=active):
            with self.assertRaisesRegex(ServiceSwitchError, "not verifiably inactive"):
                self.stage.reconcile()
        self.assertEqual(json.loads(self.stage.journal.read_text())["phase"],
                         "preparing")

    def test_stage_refuses_systemd_shadowing_after_reload(self):
        calls = 0

        def shadow(*args, **kwargs):
            nonlocal calls
            if args[0] == "show" and "LoadState" in args[2]:
                calls += 1
                return "not-found" if calls == 1 else "loaded"
            if args[0] == "show" and "FragmentPath" in args[2]:
                return "/another/search/path/local-agent-linux.service"
            return self.fake_systemd(*args, **kwargs)

        with patch("linux_agent.service_install._run_systemctl",
                   side_effect=shadow):
            with self.assertRaisesRegex(ServiceSwitchError, "exact staged unit"):
                self.stage.stage()
        self.assertTrue(self.stage.unit_file.exists())
        self.assertEqual(json.loads(self.stage.journal.read_text())["phase"],
                         "preparing")

    def test_withdraw_retains_intent_when_systemd_still_has_unit(self):
        with patch("linux_agent.service_install._run_systemctl",
                   side_effect=self.fake_systemd):
            self.stage.stage()

        def still_loaded(*args, **kwargs):
            if args[0] == "show" and "LoadState" in args[2]:
                return "loaded"
            return self.fake_systemd(*args, **kwargs)

        with patch("linux_agent.service_install._run_systemctl",
                   side_effect=still_loaded):
            with self.assertRaisesRegex(ServiceSwitchError,
                                        "still knows withdrawn unit"):
                self.stage.withdraw()
        self.assertFalse(self.stage.unit_file.exists())
        self.assertEqual(json.loads(self.stage.journal.read_text())["phase"],
                         "withdrawing")

    def test_missing_stage_file_with_active_unit_cannot_be_reconciled(self):
        with patch("linux_agent.service_install._run_systemctl",
                   side_effect=self.failing_reload):
            with self.assertRaises(ServiceSwitchError):
                self.stage.stage()
        self.stage.unit_file.unlink()

        def active_manager(*args, **kwargs):
            if args[0] == "show" and "ActiveState" in args[2]:
                return "active"
            return self.fake_systemd(*args, **kwargs)

        with patch("linux_agent.service_install._run_systemctl",
                   side_effect=active_manager):
            with self.assertRaisesRegex(ServiceSwitchError,
                                        "still knows withdrawn unit"):
                self.stage.reconcile()
        self.assertEqual(json.loads(self.stage.journal.read_text())["phase"],
                         "preparing")

    def test_external_cli_preview_calls_no_activation(self):
        import io
        from contextlib import redirect_stdout
        from linux_agent.service_install import main
        pair = self.stage.pair
        argv = [
            "preview", "--candidate-root", str(pair.candidate),
            "--candidate-sha", pair.candidate_sha,
            "--rollback-root", str(pair.rollback),
            "--rollback-sha", pair.rollback_sha,
            "--config", str(pair.config), "--state", str(pair.state),
        ]
        buf = io.StringIO()
        with patch("linux_agent.service_install.PersistentUnitStage",
                   return_value=self.stage), redirect_stdout(buf):
            self.assertEqual(main(argv), 0)
        self.assertEqual(json.loads(buf.getvalue())["status"],
                         "ready_to_stage_not_installed")
        self.assertFalse(self.stage.unit_dir.exists())
        self.assertFalse(self.stage.journal.exists())

    def test_disabled_systemd_exit_code_one_is_valid(self):
        completed = subprocess.CompletedProcess(
            ["systemctl"], 1, stdout="disabled\n", stderr=""
        )
        with patch("linux_agent.service_install.subprocess.run",
                   return_value=completed):
            self.assertEqual(_run_systemctl("is-enabled", UNIT_NAME), "disabled")
        active = subprocess.CompletedProcess(
            ["systemctl"], 0, stdout="enabled\n", stderr=""
        )
        with patch("linux_agent.service_install.subprocess.run",
                   return_value=active):
            self.assertEqual(_run_systemctl("is-enabled", UNIT_NAME), "enabled")


if __name__ == "__main__":
    unittest.main()
