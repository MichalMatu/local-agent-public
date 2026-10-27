from __future__ import annotations

from pathlib import Path
import shutil
import tempfile
import unittest

from scripts import isolated_rollout_controller as rollout


class IsolatedRolloutTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="la-rollout-test-")
        self.addCleanup(self.tmp.cleanup)
        self.parent = Path(self.tmp.name)
        self.root = self.parent / "la-rollout-fixture"
        self.root.mkdir()
        (self.root / rollout.MARKER).write_text(rollout.MARKER_VALUE)
        self.base = self.parent / "base"
        self.next = self.parent / "candidate"
        self.base.mkdir()
        for path in sorted(rollout.CHANGED):
            f = self.base / path
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text("original\n")
        shutil.copytree(self.base, self.next, dirs_exist_ok=True)
        for path in sorted(rollout.CHANGED):
            (self.next / path).write_text("new\n")
        for path in sorted(rollout.ADDED):
            f = self.next / path
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text("new test\n")
        self.before = {"daemon_state": "idle", "active_tasks": 0,
                       "pending_tasks": 0, "unreconciled_unknown": 0,
                       "source_sha": rollout.BASE_SHA}
        self.healthy = {"service_active": True, "heartbeat_fresh": True,
                        "source_digest_ok": True, "queues_safe": True}

    def test_exact_overlay_prepare_and_accept(self):
        state = rollout.prepare(self.root, self.base, self.next)
        self.assertEqual(state["state"], "prepared")
        self.assertEqual((self.root / "current").resolve(), (self.root / "snapshots/baseline").resolve())
        rollout.arm(self.root, self.before)
        self.assertEqual((self.root / "current").resolve(), (self.root / "snapshots/candidate").resolve())
        self.assertEqual(rollout.watchdog(self.root, self.healthy)["state"], "accepted")
        self.assertEqual(rollout.recover_unverified(self.root)["state"], "accepted")

    def test_no_unknown_replay_or_pending_queue(self):
        rollout.prepare(self.root, self.base, self.next)
        for field in ("active_tasks", "pending_tasks", "unreconciled_unknown"):
            with self.subTest(field=field):
                bad = self.before | {field: 1}
                with self.assertRaises(rollout.RolloutRejected):
                    rollout.arm(self.root, bad)
        self.assertEqual((self.root / "current").resolve(), (self.root / "snapshots/baseline").resolve())

    def test_watchdog_failure_rolls_back(self):
        rollout.prepare(self.root, self.base, self.next)
        rollout.arm(self.root, self.before)
        self.assertEqual(rollout.watchdog(self.root, {"service_active": False})["state"], "rolled_back")
        self.assertEqual((self.root / "current").resolve(), (self.root / "snapshots/baseline").resolve())

    def test_watchdog_expiry_rolls_back(self):
        rollout.prepare(self.root, self.base, self.next)
        state = rollout.arm(self.root, self.before)
        actual = rollout.watchdog(self.root, self.healthy, now_epoch=state["deadline_epoch"] + 1)
        self.assertEqual(actual["rollback_reason"], "watchdog_deadline_expired")

    def test_crash_recovery_never_auto_accepts(self):
        rollout.prepare(self.root, self.base, self.next)
        rollout.arm(self.root, self.before)
        actual = rollout.recover_unverified(self.root)
        self.assertEqual(actual["rollback_reason"], "unverified_interrupted_transaction")
        self.assertEqual((self.root / "current").resolve(), (self.root / "snapshots/baseline").resolve())

    def test_rejects_snapshot_mutation(self):
        rollout.prepare(self.root, self.base, self.next)
        f = self.root / "snapshots/candidate/local_agent/daemon/service.py"
        f.chmod(0o644)
        f.write_text("tampered\n")
        with self.assertRaisesRegex(rollout.RolloutRejected, "snapshot content or permissions"):
            rollout.arm(self.root, self.before)

    def test_corrupt_candidate_after_switch_rolls_back(self):
        rollout.prepare(self.root, self.base, self.next)
        rollout.arm(self.root, self.before)
        f = self.root / "snapshots/candidate/local_agent/daemon/service.py"
        f.chmod(0o644)
        f.write_text("tampered after switch\n")
        state = rollout.watchdog(self.root, self.healthy)
        self.assertEqual(state["state"], "rolled_back")
        self.assertEqual(state["rollback_reason"], "candidate_snapshot_integrity_failed")
        self.assertEqual((self.root / "current").resolve(), (self.root / "snapshots/baseline").resolve())

    def test_corrupt_baseline_blocks_untrusted_rollback(self):
        rollout.prepare(self.root, self.base, self.next)
        rollout.arm(self.root, self.before)
        f = self.root / "snapshots/baseline/local_agent/daemon/service.py"
        f.chmod(0o644)
        f.write_text("tampered baseline\n")
        with self.assertRaisesRegex(rollout.RolloutRejected, "rollback baseline"):
            rollout.watchdog(self.root, {"service_active": False})

    def test_executable_mode_preserved_and_tamper_rolls_back(self):
        relative = "local_agent/foundation/storage.py"
        (self.base / relative).chmod(0o755)
        (self.next / relative).chmod(0o755)
        state = rollout.prepare(self.root, self.base, self.next)
        self.assertEqual(state["candidate_modes"][relative], 0o555)
        rollout.arm(self.root, self.before)
        (self.root / "snapshots/candidate" / relative).chmod(0o444)
        final = rollout.watchdog(self.root, self.healthy)
        self.assertEqual(final["rollback_reason"], "candidate_snapshot_integrity_failed")
        self.assertEqual(
            (self.root / "current").resolve(),
            (self.root / "snapshots/baseline").resolve(),
        )

    def test_rejects_wrong_overlay(self):
        (self.next / "unexpected.py").write_text("unexpected\n")
        with self.assertRaisesRegex(rollout.RolloutRejected, "overlay mismatch"):
            rollout.prepare(self.root, self.base, self.next)

    def test_rejects_symlink_and_unsafe_root(self):
        (self.next / "link").symlink_to(self.base)
        with self.assertRaisesRegex(rollout.RolloutRejected, "symlink"):
            rollout.prepare(self.root, self.base, self.next)
        with self.assertRaises(rollout.RolloutRejected):
            rollout.prepare(self.parent, self.base, self.next)

    def test_rejects_invalid_before_snapshot(self):
        rollout.prepare(self.root, self.base, self.next)
        with self.assertRaisesRegex(rollout.RolloutRejected, "incomplete"):
            rollout.arm(self.root, {})


if __name__ == "__main__":
    unittest.main()
