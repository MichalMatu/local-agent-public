"""Mac-only offline launchd recovery plan, independent stable source pins."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import plistlib
import shutil
import subprocess
import sys
import tempfile
import unittest

from scripts import isolated_rollout_controller as rollout
from scripts import isolated_rollout_fence as sandbox_fence
from scripts import macos_launchd_recovery_plan as plan


@unittest.skipUnless(sys.platform == "darwin", "Mac-only offline launchd evidence")
class MacOfflineBootRecoveryPlanTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="la-rollout-macos-launchd-plan-")
        self.addCleanup(temp.cleanup)
        self.root_dir = Path(temp.name).resolve(strict=True)
        self.sandbox = self.root_dir / "la-rollout-mac-boot-policy"
        self.sandbox.mkdir(mode=0o700)
        (self.sandbox / rollout.MARKER).write_text(rollout.MARKER_VALUE)
        self.stable = self.root_dir / "trusted-standalone-recovery"
        self.stable.mkdir(mode=0o700)
        scripts = self.stable / "scripts"
        scripts.mkdir(mode=0o700)
        self.pins = {}
        for filename in plan._PINNED_MODULES:
            target = self.stable / filename
            data = f"# stable independent recovery module: {filename}\n".encode()
            target.write_bytes(data)
            target.chmod(0o600)
            self.pins[filename] = hashlib.sha256(data).hexdigest()
        self.python = Path(sys.executable)

    def render(self):
        return plan.render_disabled_boot_recovery_plan(
            sandbox=self.sandbox, python_executable=self.python,
            stable_checkout=self.stable, trusted_module_sha256=self.pins,
        )

    def verify(self, payload):
        return plan.inspect_disabled_boot_recovery_plan(
            payload, sandbox=self.sandbox, python_executable=self.python,
            stable_checkout=self.stable, trusted_module_sha256=self.pins,
        )

    def test_offline_plist_has_login_policy_but_is_disabled(self):
        payload = self.render()
        config = plistlib.loads(payload)
        self.assertTrue(config["Disabled"])
        self.assertTrue(config["RunAtLoad"])
        self.assertFalse(config["KeepAlive"])
        self.assertEqual(config["StartInterval"], 30)
        self.assertEqual(config["ProgramArguments"][1:4],
                         ["-B", "-m", "scripts.macos_rollout_boot_recovery"])
        self.assertEqual(config["ProgramArguments"][4], str(self.sandbox.resolve()))
        self.assertEqual(config["WorkingDirectory"], str(self.stable))
        self.assertNotIn(str(self.sandbox / "current"), config["WorkingDirectory"])
        self.assertFalse((self.root_dir / "LaunchAgents").exists())
        self.assertFalse((self.sandbox / "watchdog.plist").exists())
        result = self.verify(payload)
        self.assertTrue(result["source_pins_verified"])
        self.assertTrue(result["recovery_source_outside_candidate"])
        self.assertTrue(result["login_start_policy_rehearsed"])
        self.assertFalse(result["installed"])
        self.assertFalse(result["boot_tested"])
        self.assertFalse(result["source_switch_authorized"])

    def test_separate_pinned_recovery_process_restores_corrupt_candidate(self):
        # Copy the actual checked-out source modules into a standalone stable
        # checkout. They remain independent of the mutable candidate pointer.
        checked_out = Path(__file__).resolve().parents[1]
        for relative in plan._PINNED_MODULES:
            module = (checked_out / relative).read_bytes()
            target = self.stable / relative
            target.write_bytes(module)
            target.chmod(0o600)
            self.pins[relative] = hashlib.sha256(module).hexdigest()
        interpreter_plan = plistlib.loads(self.render())
        base = self.root_dir / "base"
        candidate = self.root_dir / "candidate"
        base.mkdir()
        for name in rollout.CHANGED:
            path = base / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("trusted baseline\n")
        shutil.copytree(base, candidate)
        for name in rollout.CHANGED | rollout.ADDED:
            path = candidate / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("unverified candidate\n")
        (self.sandbox / "simulated-inbox").mkdir()
        rollout.prepare(self.sandbox, base, candidate)
        before = {
            "daemon_state": "idle", "active_tasks": 0,
            "pending_tasks": 0, "unreconciled_unknown": 0,
            "source_sha": rollout.BASE_SHA,
        }
        sandbox_fence.freeze_and_switch_simulation(self.sandbox, before)
        tampered = self.sandbox / "snapshots/candidate/local_agent/daemon/service.py"
        tampered.chmod(0o644)
        tampered.write_text("corrupt candidate\n")
        env = dict(os.environ, PYTHONPATH="", PYTHONDONTWRITEBYTECODE="1")
        result = subprocess.run(
            interpreter_plan["ProgramArguments"],
            cwd=interpreter_plan["WorkingDirectory"],
            env=env, capture_output=True, text=True, timeout=12, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(report["transaction_state"], "rolled_back")
        self.assertEqual(report["pointer"], "snapshots/baseline")
        self.assertTrue(report["admission_frozen"])
        self.assertFalse(report["intake_reopened"])
        self.assertFalse(report["replay_performed"])
        self.assertEqual(rollout._pointer(self.sandbox), "snapshots/baseline")

    def test_offline_source_pin_mismatch_rejects_before_config(self):
        name = plan._PINNED_MODULES[0]
        (self.stable / name).write_text("tampered source\n")
        with self.assertRaisesRegex(rollout.RolloutRejected, "pin mismatch"):
            self.render()

    def test_missing_and_extra_source_pins_rejected(self):
        name = plan._PINNED_MODULES[0]
        original = self.pins.pop(name)
        with self.assertRaisesRegex(rollout.RolloutRejected, "incomplete"):
            self.render()
        self.pins[name] = original
        self.pins["not-a-required-module.py"] = original
        with self.assertRaisesRegex(rollout.RolloutRejected, "incomplete"):
            self.render()

    def test_unpinned_python_package_initializer_rejected(self):
        target = self.stable / "scripts/__init__.py"
        target.write_text("import os\n")
        target.chmod(0o600)
        with self.assertRaisesRegex(rollout.RolloutRejected, "unexpected.*initializer"):
            self.render()

    def test_disabled_flag_and_working_directory_cannot_be_changed(self):
        original = plistlib.loads(self.render())
        for changes in (
            {"Disabled": False},
            {"RunAtLoad": False},
            {"KeepAlive": True},
            {"WorkingDirectory": str(self.sandbox / "snapshots/candidate")},
            {"ProgramArguments": ["/bin/sh", "-c", "false"]},
        ):
            with self.subTest(changes=changes):
                edited = dict(original, **changes)
                with self.assertRaisesRegex(rollout.RolloutRejected, "policy/source"):
                    self.verify(plistlib.dumps(edited))

    def test_candidate_corruption_does_not_change_stable_source_plan(self):
        first = self.render()
        candidate = self.sandbox / "snapshots/candidate"
        candidate.mkdir(parents=True)
        (candidate / "run.py").write_text("broken candidate\n")
        self.assertEqual(self.render(), first)
        self.assertFalse(self.verify(self.render())["boot_tested"])

    def test_checkout_redirect_symlink_is_rejected(self):
        destination = self.root_dir / "separate-stable-checkout"
        self.stable.rename(destination)
        self.stable.symlink_to(destination, target_is_directory=True)
        with self.assertRaisesRegex(rollout.RolloutRejected, "independent"):
            self.render()

    def test_checkout_inside_candidate_is_rejected(self):
        candidate = self.sandbox / "snapshots" / "candidate"
        candidate.mkdir(parents=True)
        with self.assertRaisesRegex(rollout.RolloutRejected, "independent"):
            plan.render_disabled_boot_recovery_plan(
                sandbox=self.sandbox, python_executable=self.python,
                stable_checkout=candidate, trusted_module_sha256=self.pins,
            )

    def test_world_readable_recovery_checkout_is_rejected(self):
        self.stable.chmod(0o755)
        with self.assertRaisesRegex(rollout.RolloutRejected, "private"):
            self.render()

    def test_missing_python_interpreter_rejected(self):
        with self.assertRaisesRegex(rollout.RolloutRejected, "interpreter missing"):
            plan.render_disabled_boot_recovery_plan(
                sandbox=self.sandbox, python_executable=self.root_dir / "missing-python",
                stable_checkout=self.stable, trusted_module_sha256=self.pins,
            )

    def test_invalid_rehearsal_sandbox_rejected(self):
        with self.assertRaises(rollout.RolloutRejected):
            plan.render_disabled_boot_recovery_plan(
                sandbox=self.root_dir, python_executable=self.python,
                stable_checkout=self.stable, trusted_module_sha256=self.pins,
            )

    def test_invalid_or_oversized_plist_rejected(self):
        for payload in (b"", b"x" * 20000, b"not a plist", b"\x00"):
            with self.subTest(length=len(payload)):
                with self.assertRaises(rollout.RolloutRejected):
                    self.verify(payload)


if __name__ == "__main__":
    unittest.main()
