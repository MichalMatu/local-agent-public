from __future__ import annotations

from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from scripts import isolated_rollout_controller as rollout
from scripts import isolated_rollout_fence as fence


class IsolatedAdmissionFenceTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="la-rollout-fence-test-")
        self.addCleanup(temp.cleanup)
        self.parent = Path(temp.name)
        self.root = self.parent / "la-rollout-fence"
        self.root.mkdir()
        (self.root / rollout.MARKER).write_text(rollout.MARKER_VALUE)
        (self.root / "simulated-inbox").mkdir()
        self.base = self.parent / "base"
        self.candidate = self.parent / "candidate"
        self.base.mkdir()
        for name in rollout.CHANGED:
            path = self.base / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("old\n")
        shutil.copytree(self.base, self.candidate)
        for name in rollout.CHANGED | rollout.ADDED:
            path = self.candidate / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("new\n")
        rollout.prepare(self.root, self.base, self.candidate)
        self.before = {
            "daemon_state": "idle", "active_tasks": 0, "pending_tasks": 0,
            "unreconciled_unknown": 0, "source_sha": rollout.BASE_SHA,
        }

    def test_frozen_admission_precedes_switch_and_blocks_tasks(self):
        state = fence.freeze_and_switch_simulation(self.root, self.before)
        self.assertEqual(state["state"], "switched_unverified")
        self.assertTrue((self.root / "admission-frozen.json").is_file())
        with self.assertRaisesRegex(rollout.RolloutRejected, "admission frozen"):
            fence.admit_simulated_task(self.root, "new-task")
        self.assertEqual(rollout._pointer(self.root), "snapshots/candidate")

    def test_existing_pending_inbox_rejects_without_switch(self):
        fence.admit_simulated_task(self.root, "pending")
        with self.assertRaisesRegex(rollout.RolloutRejected, "pending tasks"):
            fence.freeze_and_switch_simulation(self.root, self.before)
        self.assertFalse((self.root / "admission-frozen.json").exists())
        self.assertEqual(rollout._pointer(self.root), "snapshots/baseline")

    def test_failed_switch_preserves_durable_admission_fence(self):
        with mock.patch.object(rollout, "arm", side_effect=OSError("simulated crash")):
            with self.assertRaisesRegex(OSError, "simulated crash"):
                fence.freeze_and_switch_simulation(self.root, self.before)
        self.assertTrue((self.root / "admission-frozen.json").exists())
        with self.assertRaises(rollout.RolloutRejected):
            fence.admit_simulated_task(self.root, "no-replay")

    def test_rollback_does_not_automatically_reopen_intake(self):
        fence.freeze_and_switch_simulation(self.root, self.before)
        self.assertEqual(rollout.recover_unverified(self.root)["state"], "rolled_back")
        with self.assertRaisesRegex(rollout.RolloutRejected, "frozen"):
            fence.admit_simulated_task(self.root, "replay-attempt")

    def test_real_second_process_cannot_admit_while_lock_held(self):
        command = (
            "import sys;"
            "from scripts import isolated_rollout_fence as f;"
            "f.admit_simulated_task(__import__('pathlib').Path(sys.argv[1]),'other-process')"
        )
        with fence._gate(self.root):
            result = subprocess.run(
                [sys.executable, "-c", command, str(self.root)],
                capture_output=True, text=True, timeout=5, check=False,
            )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("gate is busy", result.stderr)
        self.assertFalse((self.root / "simulated-inbox/other-process.json").exists())

    def test_invalid_sandbox_and_task_names_rejected(self):
        with self.assertRaises(rollout.RolloutRejected):
            fence.admit_simulated_task(self.root, "../escape")
        with self.assertRaises(rollout.RolloutRejected):
            fence.freeze_and_switch_simulation(self.parent, self.before)


if __name__ == "__main__":
    unittest.main()
