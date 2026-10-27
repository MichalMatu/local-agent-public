"""Native Mac proof: an independent launchd process restores a disposable service.

Only opt in with LOCAL_AGENT_DISPOSABLE_LAUNCHD_TEST=1. Neither test
nor recovery script can address the installed com.michal.local-agent.
Physical logout/reboot is NOT exercised here; keep that release gate open.
"""
from __future__ import annotations

import hashlib
import json
import os
from unittest import mock
from pathlib import Path
import plistlib
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
import uuid

from scripts import macos_b1c_disposable_service_recovery as recovery


@unittest.skipUnless(sys.platform == "darwin", "macOS only")
class MacB1cDisposableRecoveryPolicyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="la-b1c-launchd-fixture-")
        self.addCleanup(self.temp.cleanup)
        self.parent = Path(self.temp.name).resolve()
        self.root = self.parent / ("la-b1c-launchd-" + uuid.uuid4().hex)
        self.root.mkdir(mode=0o700)
        (self.root / recovery.MARKER).write_bytes(recovery.MARKER_BYTES)
        self.label = recovery.LABEL_PREFIX + uuid.uuid4().hex
        self.baseline = self.root / "baseline.plist"
        self.candidate = self.root / "candidate.plist"
        self.baseline.write_bytes(plistlib.dumps(self.service("120")))
        self.candidate.write_bytes(plistlib.dumps(self.service("121")))
        self.record = self.root / recovery.JOURNAL
        self.write_journal()

    def service(self, seconds):
        return {"Label": self.label,
                "ProgramArguments": ["/bin/sleep", seconds],
                "RunAtLoad": True, "KeepAlive": True}

    def write_journal(self):
        journal = {"version": 1, "state": "armed", "label": self.label,
                   "baseline_plist_sha256": hashlib.sha256(self.baseline.read_bytes()).hexdigest(),
                   "candidate_plist_sha256": hashlib.sha256(self.candidate.read_bytes()).hexdigest()}
        self.record.write_text(json.dumps(journal, sort_keys=True) + "\n")
        for path in (self.record, self.baseline, self.candidate, self.root / recovery.MARKER):
            path.chmod(0o600)

    def test_restart_after_bootout_can_restore_from_durable_journal(self):
        record = recovery._journal(self.root)
        recovery._persist_state(self.root, record, "rollback_started")
        absent = subprocess.CompletedProcess([], 113, "", "")
        base = subprocess.CompletedProcess([], 0, "program = /bin/sleep\n 120\n", "")
        success = subprocess.CompletedProcess([], 0, "", "")
        with mock.patch.object(
            recovery, "_launchctl", side_effect=[absent, success, base],
        ) as launch:
            outcome = recovery.recover(self.root)
        self.assertEqual(outcome["state"], "rolled_back")
        self.assertFalse(outcome["tasks_replayed"])
        self.assertEqual(launch.call_count, 3)
        self.assertEqual(launch.call_args_list[1].args[0], "bootstrap")
        self.assertEqual(json.loads(self.record.read_text())["state"], "rolled_back")

    def test_restart_after_baseline_bootstrap_does_not_bootout_it_again(self):
        record = recovery._journal(self.root)
        recovery._persist_state(self.root, record, "rollback_started")
        baseline = subprocess.CompletedProcess([], 0, "program = /bin/sleep\n 120\n", "")
        with mock.patch.object(
            recovery, "_launchctl", side_effect=[baseline, baseline],
        ) as launch:
            result = recovery.recover(self.root)
        self.assertEqual(result["state"], "rolled_back")
        self.assertEqual([x.args[0] for x in launch.call_args_list], ["print", "print"])

    def test_armed_state_refuses_unrecognized_running_service(self):
        unknown = subprocess.CompletedProcess([], 0, "program = /bin/sleep\n 999\n", "")
        with mock.patch.object(recovery, "_launchctl", return_value=unknown) as launch:
            with self.assertRaisesRegex(
                recovery.DisposableRecoveryRejected, "ambiguous service",
            ):
                recovery.recover(self.root)
        self.assertEqual(launch.call_count, 1)
        self.assertEqual(json.loads(self.record.read_text())["state"], "armed")

    def test_production_label_never_allowed(self):
        data = json.loads(self.record.read_text())
        data["label"] = "com.michal.local-agent"
        self.record.write_text(json.dumps(data))
        with self.assertRaisesRegex(recovery.DisposableRecoveryRejected, "label/state invalid"):
            recovery.recover(self.root)

    def test_tampered_plist_and_symlink_never_run_launchctl(self):
        self.baseline.write_bytes(plistlib.dumps(
            {"Label": self.label, "ProgramArguments": ["/bin/echo", "unsafe"],
             "RunAtLoad": True, "KeepAlive": True}))
        with self.assertRaisesRegex(recovery.DisposableRecoveryRejected, "integrity mismatch"):
            recovery.recover(self.root)
        self.baseline.unlink()
        self.baseline.symlink_to("/tmp/not-trusted")
        with self.assertRaisesRegex(recovery.DisposableRecoveryRejected, "symlinked"):
            recovery.recover(self.root)


@unittest.skipUnless(
    sys.platform == "darwin" and
    os.environ.get("LOCAL_AGENT_DISPOSABLE_LAUNCHD_TEST") == "1",
    "Explicit opt-in required for real isolated launchd bootstrap",
)
class MacB1cDisposableLaunchdServiceRecoveryTests(MacB1cDisposableRecoveryPolicyTests):
    def test_launchd_independently_restores_actual_candidate_service(self):
        domain = f"gui/{os.getuid()}"
        target = f"{domain}/{self.label}"
        source = self.parent / "stable-recovery" / "scripts"
        source.mkdir(parents=True, mode=0o700)
        stable = source / "macos_b1c_disposable_service_recovery.py"
        shutil.copy2(Path(recovery.__file__), stable)
        stable.chmod(0o600)
        self.assertEqual(
            hashlib.sha256(stable.read_bytes()).hexdigest(),
            hashlib.sha256(Path(recovery.__file__).read_bytes()).hexdigest(),
        )
        independent_label = "dev.localagent.macos-b1c.recovery." + uuid.uuid4().hex
        out = self.parent / "watchdog.out"
        err = self.parent / "watchdog.err"
        watchdog = self.parent / "external-recovery-job.plist"
        watchdog.write_bytes(plistlib.dumps({
            "Label": independent_label,
            "ProgramArguments": [
                sys.executable, "-B", str(stable), str(self.root),
            ],
            "RunAtLoad": True,
            "KeepAlive": False,
            "StandardOutPath": str(out),
            "StandardErrorPath": str(err),
        }))
        watchdog.chmod(0o600)
        started_candidate = False
        started_watchdog = False
        try:
            begin = subprocess.run(
                ["/bin/launchctl", "bootstrap", domain, str(self.candidate)],
                text=True, capture_output=True, timeout=15,
            )
            self.assertEqual(begin.returncode, 0, begin.stderr)
            started_candidate = True
            self.assertEqual(subprocess.run(
                ["/bin/launchctl", "print", target],
                capture_output=True, timeout=10,
            ).returncode, 0)
            run = subprocess.run(
                ["/bin/launchctl", "bootstrap", domain, str(watchdog)],
                capture_output=True, text=True, timeout=15,
            )
            self.assertEqual(run.returncode, 0, run.stderr)
            started_watchdog = True
            status = subprocess.run(
                ["/bin/launchctl", "print", f"{domain}/{independent_label}"],
                capture_output=True, text=True, timeout=10,
            )
            self.assertEqual(status.returncode, 0, status.stderr)
            deadline = time.monotonic() + 22
            report = None
            while time.monotonic() < deadline:
                if out.exists() and out.stat().st_size:
                    try:
                        report = json.loads(out.read_text())
                        break
                    except (json.JSONDecodeError, UnicodeError):
                        pass
                time.sleep(0.1)
            stderr = err.read_text() if err.exists() else ""
            self.assertIsNotNone(report, f"independent recovery did not finish: {stderr}")
            self.assertEqual(report["state"], "rolled_back")
            self.assertTrue(report["real_launchd_restore"])
            self.assertFalse(report["production_service_changed"])
            self.assertFalse(report["tasks_replayed"])
            self.assertEqual(json.loads(self.record.read_text())["state"], "rolled_back")
            restored = subprocess.run(
                ["/bin/launchctl", "print", target],
                capture_output=True, text=True, timeout=10,
            )
            self.assertEqual(restored.returncode, 0, restored.stderr)
            self.assertIn("120", restored.stdout)
        finally:
            if started_watchdog:
                subprocess.run(["/bin/launchctl", "bootout", f"{domain}/{independent_label}"],
                               capture_output=True, timeout=15)
            if started_candidate:
                subprocess.run(["/bin/launchctl", "bootout", target],
                               capture_output=True, timeout=15)


if __name__ == "__main__":
    unittest.main()
