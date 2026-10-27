"""Real launchd lifecycle proof for a disposable Mac B1c rollback only.

Opt in with LOCAL_AGENT_DISPOSABLE_LAUNCHD_TEST=1. Never bootstrap the
installed Local Agent label or write to the user's LaunchAgents directory.
A successful test proves launchd executed a recovery job, NOT reboot
persistence, trusted live deployment, or permission to unfreeze intake.
"""
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
import time
import unittest
import uuid

from scripts import isolated_rollout_controller as rollout
from scripts import isolated_rollout_fence as fence
from scripts import macos_launchd_recovery_plan as plan

_LABEL_PREFIX = "dev.localagent.macos-b1c.disposable."


@unittest.skipUnless(
    sys.platform == "darwin"
    and os.environ.get("LOCAL_AGENT_DISPOSABLE_LAUNCHD_TEST") == "1",
    "Requires explicitly opted-in isolated macOS launchd session",
)
class MacLaunchdDisposableLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="la-launchd-test-")
        self.addCleanup(self.tmp.cleanup)
        self.parent = Path(self.tmp.name).resolve(strict=True)
        self.sandbox = self.parent / "la-rollout-macos-launchd-disposable"
        self.sandbox.mkdir(mode=0o700)
        (self.sandbox / rollout.MARKER).write_text(rollout.MARKER_VALUE)
        (self.sandbox / "simulated-inbox").mkdir(mode=0o700)
        self.stable = self.parent / "trusted-stable-recovery"
        self.stable.mkdir(mode=0o700)
        (self.stable / "scripts").mkdir(mode=0o700)
        self.pins = {}
        source = Path(__file__).resolve().parents[1]
        for relative in plan._PINNED_MODULES:
            data = (source / relative).read_bytes()
            target = self.stable / relative
            target.write_bytes(data)
            target.chmod(0o600)
            self.pins[relative] = hashlib.sha256(data).hexdigest()
        base = self.parent / "baseline"
        candidate = self.parent / "candidate"
        base.mkdir(mode=0o700)
        for name in rollout.CHANGED:
            path = base / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("installed baseline\n")
        shutil.copytree(base, candidate)
        for name in rollout.CHANGED | rollout.ADDED:
            path = candidate / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("unverified candidate\n")
        rollout.prepare(self.sandbox, base, candidate)
        before = {
            "daemon_state": "idle",
            "active_tasks": 0,
            "pending_tasks": 0,
            "unreconciled_unknown": 0,
            "source_sha": rollout.BASE_SHA,
        }
        fence.freeze_and_switch_simulation(self.sandbox, before)
        self.assertEqual(rollout._pointer(self.sandbox), "snapshots/candidate")
        self.assertTrue((self.sandbox / fence._FENCE).exists())

    def _verified_template(self):
        data = plan.render_disabled_boot_recovery_plan(
            sandbox=self.sandbox,
            python_executable=Path(sys.executable),
            stable_checkout=self.stable,
            trusted_module_sha256=self.pins,
        )
        proof = plan.inspect_disabled_boot_recovery_plan(
            data,
            sandbox=self.sandbox,
            python_executable=Path(sys.executable),
            stable_checkout=self.stable,
            trusted_module_sha256=self.pins,
        )
        self.assertFalse(proof["installed"])
        return plistlib.loads(data)

    def test_launchd_executes_independent_corrupt_candidate_rollback(self):
        # Retain the immutable protected baseline but damage the unaccepted
        # candidate. The new launchd job runs from the OTHER, pinned checkout.
        candidate = self.sandbox / "snapshots/candidate/local_agent/daemon/service.py"
        candidate.chmod(0o644)
        candidate.write_text("corrupt unaccepted candidate\n")
        configuration = self._verified_template()
        label = _LABEL_PREFIX + uuid.uuid4().hex
        configuration["Label"] = label
        configuration.pop("Disabled")
        configuration.pop("StartInterval")
        configuration["StandardOutPath"] = str(self.parent / "recovery.out")
        configuration["StandardErrorPath"] = str(self.parent / "recovery.err")
        plist = self.parent / "disposable-launchd.plist"
        plist.write_bytes(plistlib.dumps(configuration))
        plist.chmod(0o600)
        domain = f"gui/{os.getuid()}"
        bootstrapped = False
        try:
            launched = subprocess.run(
                ["/bin/launchctl", "bootstrap", domain, str(plist)],
                capture_output=True, text=True, timeout=12, check=False,
            )
            self.assertEqual(launched.returncode, 0, launched.stderr)
            bootstrapped = True
            inspected = subprocess.run(
                ["/bin/launchctl", "print", f"{domain}/{label}"],
                capture_output=True, text=True, timeout=8, check=False,
            )
            self.assertEqual(inspected.returncode, 0, inspected.stderr)
            deadline = time.monotonic() + 15
            report = None
            while time.monotonic() < deadline:
                output = self.parent / "recovery.out"
                if output.is_file() and output.stat().st_size:
                    try:
                        report = json.loads(output.read_text())
                        break
                    except json.JSONDecodeError:
                        pass
                time.sleep(0.1)
            errors = self.parent / "recovery.err"
            stderr = errors.read_text()[:2500] if errors.is_file() else ""
            self.assertIsNotNone(report, f"launchd job did not publish a report: {stderr}")
            self.assertEqual(report["transaction_state"], "rolled_back")
            self.assertEqual(report["pointer"], "snapshots/baseline")
            self.assertTrue(report["admission_frozen"])
            self.assertFalse(report["service_restart_performed"])
            self.assertFalse(report["replay_performed"])
            self.assertFalse(report["intake_reopened"])
            self.assertEqual(rollout._pointer(self.sandbox), "snapshots/baseline")
        finally:
            if bootstrapped:
                stopped = subprocess.run(
                    ["/bin/launchctl", "bootout", domain, str(plist)],
                    capture_output=True, text=True, timeout=12, check=False,
                )
                self.assertEqual(stopped.returncode, 0, stopped.stderr)

    def test_invalid_stable_source_is_rejected_before_any_launchd_job(self):
        target = self.stable / plan._PINNED_MODULES[0]
        target.chmod(0o600)
        target.write_text("tampered\n")
        with self.assertRaisesRegex(rollout.RolloutRejected, "pin mismatch"):
            self._verified_template()
        self.assertEqual(rollout._pointer(self.sandbox), "snapshots/candidate")


if __name__ == "__main__":
    unittest.main()
