"""Cross-process Mac test of admission lock producer; never touches installed state."""
from __future__ import annotations

from pathlib import Path
import json
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from local_agent.supervisor import macos_release_admission as gate
from scripts import isolated_rollout_controller as rollout
from scripts import macos_release_fence_rehearsal as rehearsal


@unittest.skipUnless(sys.platform == "darwin", "Mac-only native acceptance")
class DisposableMacReleaseFenceTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="la-rollout-macos-admission-")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name) / "la-rollout-macos-release"
        self.root.mkdir()
        (self.root / rollout.MARKER).write_text(rollout.MARKER_VALUE)
        self.state = rehearsal.stage_disposable_opt_in(self.root)

    def test_exclusive_fence_waits_for_shared_worker(self):
        code = "\n".join([
            "import sys",
            "from pathlib import Path",
            "from local_agent.supervisor.macos_release_admission import macos_task_admission",
            "with macos_task_admission(Path(sys.argv[1])):",
            "    print('READY', flush=True)",
            "    sys.stdin.readline()",
        ])
        process = subprocess.Popen(
            [sys.executable, "-c", code, str(self.state)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True,
        )
        try:
            self.assertEqual(process.stdout.readline().strip(), "READY")
            with self.assertRaisesRegex(rollout.RolloutRejected, "active Mac test task"):
                rehearsal.freeze_disposable_mac_admission(self.root)
            self.assertFalse((self.state / gate.FROZEN).exists())
            process.stdin.write("\n")
            process.stdin.flush()
            out, err = process.communicate(timeout=7)
            self.assertEqual(process.returncode, 0, err)
            self.assertEqual(out, "")
            receipt = rehearsal.freeze_disposable_mac_admission(self.root)
            self.assertTrue(receipt["admission_frozen"])
            self.assertFalse(receipt["live_mac_admission_authorized"])
            self.assertTrue((self.state / gate.FROZEN).is_file())
            with self.assertRaisesRegex(gate.MacReleaseAdmissionBlocked, "frozen"):
                with gate.macos_task_admission(self.state):
                    self.fail("post-freeze task admission impossible")
        finally:
            if process.poll() is None:
                process.kill()
                process.communicate(timeout=5)

    def test_freeze_persists_across_independent_process(self):
        code = (
            "import json,sys;"
            "from pathlib import Path;"
            "from scripts.macos_release_fence_rehearsal import "
            "freeze_disposable_mac_admission as freeze;"
            "print(json.dumps(freeze(Path(sys.argv[1]))))"
        )
        child = subprocess.run([sys.executable, "-c", code, str(self.root)],
                               capture_output=True, text=True, timeout=10, check=False)
        self.assertEqual(child.returncode, 0, child.stderr)
        receipt = json.loads(child.stdout)
        self.assertTrue(receipt["exclusive_lease_acquired"])
        self.assertTrue(receipt["admission_frozen"])
        with self.assertRaises(rollout.RolloutRejected):
            rehearsal.freeze_disposable_mac_admission(self.root)
        self.assertTrue((self.state / gate.FROZEN).is_file())

    def test_existing_marker_or_missing_config_fails_closed(self):
        rehearsal.freeze_disposable_mac_admission(self.root)
        with self.assertRaises(rollout.RolloutRejected):
            rehearsal.freeze_disposable_mac_admission(self.root)
        (self.state / gate.CONFIG).unlink()
        with self.assertRaises(gate.MacReleaseAdmissionBlocked):
            rehearsal.freeze_disposable_mac_admission(self.root)
        with self.assertRaises(gate.MacReleaseAdmissionBlocked):
            with gate.macos_task_admission(self.state):
                self.fail("orphaned freeze cannot allow a task")

    def test_wrong_root_or_symlinked_state_rejected(self):
        with self.assertRaises(rollout.RolloutRejected):
            rehearsal.freeze_disposable_mac_admission(self.root.parent)
        self.state.rename(self.root / "held")
        self.state.symlink_to(self.root / "held", target_is_directory=True)
        with self.assertRaisesRegex(rollout.RolloutRejected, "missing"):
            rehearsal.freeze_disposable_mac_admission(self.root)

    def test_world_readable_directory_rejected(self):
        self.state.chmod(0o755)
        with self.assertRaisesRegex(rollout.RolloutRejected, "not private"):
            rehearsal.freeze_disposable_mac_admission(self.root)

    def test_linked_lock_rejected_without_mutation(self):
        source = self.root / "foreign-lock"
        source.write_text("no")
        (self.state / gate.LOCK).symlink_to(source)
        with self.assertRaisesRegex(rollout.RolloutRejected, "symlink"):
            rehearsal.freeze_disposable_mac_admission(self.root)
        self.assertFalse((self.state / gate.FROZEN).exists())
        self.assertEqual(source.read_text(), "no")

    def test_inert_on_non_macos(self):
        with mock.patch.object(rehearsal.sys, "platform", "linux"):
            with self.assertRaisesRegex(rollout.RolloutRejected, "macOS-only"):
                rehearsal.freeze_disposable_mac_admission(self.root)

    def test_frozen_admission_never_replays_unknown_ledger(self):
        claim = self.state / "claims" / "interrupted.json"
        claim.parent.mkdir()
        claim.write_text('{"status":"unknown"}\n')
        rehearsal.freeze_disposable_mac_admission(self.root)
        self.assertEqual(claim.read_text(), '{"status":"unknown"}\n')
        self.assertFalse((self.state / "results").exists())


if __name__ == "__main__":
    unittest.main()
