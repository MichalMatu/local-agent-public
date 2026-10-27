from __future__ import annotations

import json
from pathlib import Path
import plistlib
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from scripts import isolated_rollout_controller as rollout
from scripts import isolated_rollout_fence as fence
from scripts.macos_launchd_probe import inspect_launchagent
from scripts.macos_rollout_boot_recovery import recover_after_process_or_boot


class MacLaunchdReadOnlyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="la-rollout-mac-service-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.source = self.root / "source-cccc67fc"
        self.source.mkdir()
        (self.source / "runner.py").write_text("pass\n")
        self.plist = self.root / "test.plist"
        self.write_plist()

    def write_plist(self, **changes):
        data = {
            "Label": "test.localagent.readonly",
            "RunAtLoad": True,
            "ProgramArguments": [sys.executable, str(self.source / "runner.py")],
        }
        data.update(changes)
        self.plist.write_bytes(plistlib.dumps(data))

    def run_probe(self, *, output="state = running\npid = 3212\n", exitcode=0):
        result = subprocess.CompletedProcess(["launchctl"], exitcode, output, "")
        return inspect_launchagent(
            plist_path=self.plist, label="test.localagent.readonly",
            expected_source=self.source, uid=500,
            runner=lambda *args, **kwargs: result, verify_pid=False,
        )

    def test_running_service_and_source_are_observational_only(self):
        data = self.run_probe()
        self.assertEqual(data["pid"], 3212)
        self.assertTrue(data["service_running"])
        self.assertFalse(data["source_switch_authorized"])
        self.assertFalse(data["admission_fenced"])

    def test_wrong_source_or_disabled_service_fails_closed(self):
        for override in (
            {"ProgramArguments": [sys.executable, str(self.root / "other.py")]},
            {"RunAtLoad": False},
            {"Disabled": True},
            {"Label": "other.label"},
        ):
            with self.subTest(override=override):
                self.write_plist(**override)
                with self.assertRaises(rollout.RolloutRejected):
                    self.run_probe()

    def test_source_substring_spoof_is_rejected(self):
        self.write_plist(ProgramArguments=[
            sys.executable, str(self.root / "source-cccc67fc-spoof/runner.py")
        ])
        with self.assertRaisesRegex(rollout.RolloutRejected, "not anchored"):
            self.run_probe()

    def test_symlinked_plist_and_launchd_state_rejected(self):
        self.plist.unlink()
        self.plist.symlink_to(self.source / "runner.py")
        with self.assertRaisesRegex(rollout.RolloutRejected, "symlink"):
            self.run_probe()
        self.plist.unlink()
        self.write_plist()
        for status in ("state = waiting\npid = 3212\n", "state = running\npid = 0\n"):
            with self.subTest(status=status):
                with self.assertRaises(rollout.RolloutRejected):
                    self.run_probe(output=status)

    def test_invalid_launcher_pid_cannot_claim_liveness(self):
        with mock.patch("scripts.macos_launchd_probe.os.kill", side_effect=ProcessLookupError):
            with self.assertRaisesRegex(rollout.RolloutRejected, "liveness"):
                inspect_launchagent(
                    plist_path=self.plist, label="test.localagent.readonly",
                    expected_source=self.source,
                    runner=lambda *a, **kw: subprocess.CompletedProcess(
                        [], 0, "state = running\npid = 3212\n", ""),
                )

    def test_launchctl_failure_or_malformed_pid_rejected(self):
        with self.assertRaises(rollout.RolloutRejected):
            self.run_probe(output="", exitcode=1)
        with self.assertRaises(rollout.RolloutRejected):
            self.run_probe(output="state = running\npid = invalid\n")


class MacBootStyleRollbackTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="la-rollout-mac-boot-")
        self.addCleanup(tmp.cleanup)
        self.base_parent = Path(tmp.name)
        self.root = self.base_parent / "la-rollout-boot"
        self.root.mkdir()
        (self.root / rollout.MARKER).write_text(rollout.MARKER_VALUE)
        (self.root / "simulated-inbox").mkdir()
        base = self.base_parent / "base"
        candidate = self.base_parent / "candidate"
        base.mkdir()
        for name in rollout.CHANGED:
            item = base / name
            item.parent.mkdir(parents=True, exist_ok=True)
            item.write_text("stable\n")
        shutil.copytree(base, candidate)
        for name in rollout.CHANGED | rollout.ADDED:
            item = candidate / name
            item.parent.mkdir(parents=True, exist_ok=True)
            item.write_text("candidate\n")
        rollout.prepare(self.root, base, candidate)
        self.before = {
            "daemon_state": "idle", "active_tasks": 0, "pending_tasks": 0,
            "unreconciled_unknown": 0, "source_sha": rollout.BASE_SHA,
        }

    def test_new_python_process_restores_unverified_transaction(self):
        fence.freeze_and_switch_simulation(self.root, self.before)
        done = subprocess.run(
            [sys.executable, "-m", "scripts.macos_rollout_boot_recovery",
             str(self.root)], check=False, capture_output=True, text=True, timeout=12,
        )
        self.assertEqual(done.returncode, 0, done.stderr)
        data = json.loads(done.stdout)
        self.assertEqual(data["transaction_state"], "rolled_back")
        self.assertTrue(data["admission_frozen"])
        self.assertFalse(data["intake_reopened"])
        self.assertFalse(data["replay_performed"])
        self.assertEqual(rollout._pointer(self.root), "snapshots/baseline")
        with self.assertRaisesRegex(rollout.RolloutRejected, "frozen"):
            fence.admit_simulated_task(self.root, "attempt")

    def test_recovery_is_idempotent_and_never_replays(self):
        fence.freeze_and_switch_simulation(self.root, self.before)
        first = recover_after_process_or_boot(self.root)
        second = recover_after_process_or_boot(self.root)
        self.assertEqual(first, second)
        self.assertEqual(second["transaction_state"], "rolled_back")

    def test_accepted_candidate_is_preserved_but_intake_stays_frozen(self):
        fence.freeze_and_switch_simulation(self.root, self.before)
        health = {
            "service_active": True, "heartbeat_fresh": True,
            "source_digest_ok": True, "queues_safe": True,
        }
        rollout.watchdog(self.root, health)
        result = recover_after_process_or_boot(self.root)
        self.assertEqual(result["transaction_state"], "accepted")
        self.assertEqual(result["pointer"], "snapshots/candidate")
        self.assertTrue(result["admission_frozen"])

    def test_arm_crash_before_pointer_switch_recovers_baseline(self):
        with mock.patch.object(rollout, "_set_pointer",
                               side_effect=RuntimeError("power loss before pointer")):
            with self.assertRaisesRegex(RuntimeError, "power loss"):
                fence.freeze_and_switch_simulation(self.root, self.before)
        self.assertEqual(rollout._journal(self.root)["state"], "armed")
        self.assertEqual(rollout._pointer(self.root), "snapshots/baseline")
        result = recover_after_process_or_boot(self.root)
        self.assertEqual(result["transaction_state"], "rolled_back")
        self.assertEqual(result["pointer"], "snapshots/baseline")
        self.assertTrue(result["admission_frozen"])

    def test_accepted_but_corrupt_candidate_is_not_reaccepted(self):
        fence.freeze_and_switch_simulation(self.root, self.before)
        health = {
            "service_active": True, "heartbeat_fresh": True,
            "source_digest_ok": True, "queues_safe": True,
        }
        rollout.watchdog(self.root, health)
        path = self.root / "snapshots/candidate/local_agent/daemon/service.py"
        path.chmod(0o644)
        path.write_text("corrupt candidate\n")
        with self.assertRaisesRegex(rollout.RolloutRejected, "snapshot content"):
            recover_after_process_or_boot(self.root)
        self.assertTrue((self.root / fence._FENCE).exists())

    def test_untrusted_marker_blocks_recovery(self):
        fence.freeze_and_switch_simulation(self.root, self.before)
        (self.root / fence._FENCE).write_text("{}")
        with self.assertRaisesRegex(rollout.RolloutRejected, "untrusted"):
            recover_after_process_or_boot(self.root)
        self.assertEqual(rollout._pointer(self.root), "snapshots/candidate")

    def test_fence_missing_blocks_recovery(self):
        fence.freeze_and_switch_simulation(self.root, self.before)
        (self.root / fence._FENCE).unlink()
        with self.assertRaisesRegex(rollout.RolloutRejected, "missing safe admission marker"):
            recover_after_process_or_boot(self.root)
        self.assertEqual(rollout._pointer(self.root), "snapshots/candidate")

    def test_unknown_journal_state_blocks_recovery(self):
        fence.freeze_and_switch_simulation(self.root, self.before)
        journal = self.root / rollout.JOURNAL
        data = json.loads(journal.read_text())
        data["state"] = "untrusted_pending"
        rollout._atomic_json(journal, data)
        with self.assertRaisesRegex(rollout.RolloutRejected, "unknown recovery"):
            recover_after_process_or_boot(self.root)
        self.assertEqual(rollout._pointer(self.root), "snapshots/candidate")

    def test_corrupt_protected_baseline_prevents_unsafe_rollback(self):
        fence.freeze_and_switch_simulation(self.root, self.before)
        source = self.root / "snapshots/baseline/local_agent/daemon/service.py"
        source.chmod(0o644)
        source.write_text("corrupted\n")
        with self.assertRaisesRegex(rollout.RolloutRejected, "baseline"):
            recover_after_process_or_boot(self.root)
        self.assertEqual(rollout._pointer(self.root), "snapshots/candidate")

    def test_linked_or_duplicate_recovery_journal_rejected(self):
        fence.freeze_and_switch_simulation(self.root, self.before)
        journal = self.root / rollout.JOURNAL
        original = journal.read_text()
        copy = self.base_parent / "untrusted-journal"
        copy.write_text(original)
        journal.unlink()
        journal.symlink_to(copy)
        with self.assertRaisesRegex(rollout.RolloutRejected, "symlink"):
            recover_after_process_or_boot(self.root)
        journal.unlink()
        journal.write_text('{"base_sha":"' + rollout.BASE_SHA
                           + '","state":"armed","state":"accepted"}')
        with self.assertRaisesRegex(rollout.RolloutRejected, "duplicate"):
            recover_after_process_or_boot(self.root)
        self.assertEqual(rollout._pointer(self.root), "snapshots/candidate")

    def test_insecure_admission_marker_permissions_rejected(self):
        fence.freeze_and_switch_simulation(self.root, self.before)
        (self.root / fence._FENCE).chmod(0o666)
        with self.assertRaisesRegex(rollout.RolloutRejected, "invalid frozen"):
            recover_after_process_or_boot(self.root)
        self.assertEqual(rollout._pointer(self.root), "snapshots/candidate")

    def test_malformed_journal_preserves_frozen_rollback_gate(self):
        fence.freeze_and_switch_simulation(self.root, self.before)
        journal = self.root / rollout.JOURNAL
        journal.write_text("not valid JSON")
        with self.assertRaisesRegex(rollout.RolloutRejected, "malformed"):
            recover_after_process_or_boot(self.root)
        with self.assertRaisesRegex(rollout.RolloutRejected, "frozen"):
            fence.admit_simulated_task(self.root, "unexpected")

    def test_wrong_root_rejected_before_any_effect(self):
        with self.assertRaises(rollout.RolloutRejected):
            recover_after_process_or_boot(self.base_parent)


if __name__ == "__main__":
    unittest.main()
