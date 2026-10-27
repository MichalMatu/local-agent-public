"""Native Mac fail-closed queue inventory: disposable fixtures only."""
from __future__ import annotations

import datetime as dt
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from local_agent.supervisor import macos_release_admission as gate
from scripts import isolated_rollout_controller as rollout
from scripts import macos_release_fence_rehearsal as fence
from scripts.macos_release_quiescence_rehearsal import inspect_disposable_mac_quiescence


@unittest.skipUnless(sys.platform == "darwin", "Mac-only native acceptance")
class MacQuiescenceInventoryTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="la-rollout-mac-inventory-")
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name) / "la-rollout-mac-inventory"
        self.root.mkdir()
        (self.root / rollout.MARKER).write_text(rollout.MARKER_VALUE)
        self.state = fence.stage_disposable_opt_in(self.root)
        self.project_ids = tuple(f"project-{i}" for i in range(9))
        self.mkdir(self.state / "repositories")
        self.mkdir(self.root / "mailboxes")
        for project in self.project_ids:
            for path in (
                self.state / "repositories" / project / "claims",
                self.state / "repositories" / project / "result-spool",
                self.root / "mailboxes" / project / "tasks",
                self.root / "mailboxes" / project / "results",
            ):
                self.mkdir(path)
        for name in ("requests", "results", "claims", "remote-approvals"):
            self.mkdir(self.state / "host-tasks" / name)
        for name in ("requests", "results", "launches"):
            self.mkdir(self.state / "conversation-operator" / name)
        self.write(self.state / "status.json", {
            "state": "idle", "pid": os.getpid(), "self_revision": rollout.BASE_SHA,
            "updated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        })
        # macOS may apply permissions from the parent umask to auto-created
        # intermediate directories. Normalize the entire *disposable*
        # fixture only, before the read-only audit starts.
        for directory in self.root.rglob("*"):
            if directory.is_dir():
                directory.chmod(0o700)
        fence.freeze_disposable_mac_admission(self.root)

    def mkdir(self, path: Path):
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        # parents=True uses the system umask for intermediate components.
        # Make every *sandbox-local* ancestor private, not just the leaf.
        current = path
        while current != self.root and self.root in current.parents:
            current.chmod(0o700)
            current = current.parent

    def write(self, path: Path, payload: dict):
        path.write_text(json.dumps(payload) + "\n")
        path.chmod(0o600)

    def audit(self):
        return inspect_disposable_mac_quiescence(
            self.root, project_ids=self.project_ids,
        )

    def test_full_nine_project_inventory_is_only_observational(self):
        out = self.audit()
        self.assertTrue(out["quiescent_observation"])
        self.assertEqual(out["project_count"], 9)
        self.assertEqual(len(out["projects"]), 9)
        self.assertFalse(out["source_switch_authorized"])
        self.assertFalse(out["real_queue_exclusion_proven"])
        self.assertFalse(out["tasks_replayed"])
        self.assertTrue((self.state / gate.FROZEN).exists())

    def test_pending_project_task_returns_not_quiescent(self):
        self.write(self.root / "mailboxes/project-5/tasks/pending.json", {"id": "pending"})
        snapshot = self.audit()
        self.assertEqual(snapshot["projects"]["project-5"]["pending"], 1)
        self.assertFalse(snapshot["quiescent_observation"])

    def test_orphan_claim_and_publication_spool_block(self):
        claim = self.state / "repositories/project-1/claims/unknown.json"
        spool = self.state / "repositories/project-2/result-spool/retry.json"
        self.write(claim, {"status": "unknown"})
        self.write(spool, {"id": "retry"})
        snapshot = self.audit()
        self.assertEqual(snapshot["projects"]["project-1"]["claims"], 1)
        self.assertEqual(snapshot["projects"]["project-2"]["spool"], 1)
        self.assertFalse(snapshot["quiescent_observation"])
        self.assertEqual(json.loads(claim.read_text())["status"], "unknown")

    def test_interrupted_failed_result_not_treated_as_safe(self):
        target = self.root / "mailboxes/project-0/results/old.json"
        self.write(target, {"id": "old", "status": "failed",
                            "failure_reason": "interrupted_previous_attempt",
                            "replayed": False})
        snapshot = self.audit()
        self.assertFalse(snapshot["projects"]["project-0"]["settled"])
        self.assertEqual(snapshot["projects"]["project-0"]["nonterminal"], 1)

    def test_terminal_completed_result_does_not_trigger_replay(self):
        self.write(self.root / "mailboxes/project-0/results/done.json",
                   {"id": "done", "status": "done", "replayed": False})
        snapshot = self.audit()
        self.assertTrue(snapshot["quiescent_observation"])
        self.assertFalse(snapshot["tasks_replayed"])

    def test_host_unknown_and_remote_approval_prevent_quiescence(self):
        self.write(self.state / "host-tasks/claims/executing.json", {"id": "executing"})
        self.write(self.state / "host-tasks/remote-approvals/orphan.json",
                   {"id": "orphan"})
        with self.assertRaisesRegex(rollout.RolloutRejected, "unsettled Mac remote"):
            self.audit()
        (self.state / "host-tasks/remote-approvals/orphan.json").unlink()
        self.assertFalse(self.audit()["host"]["settled"])

    def test_conversation_reserved_once_requires_manual_reconciliation(self):
        self.write(self.state / "conversation-operator/launches/once.json",
                   {"id": "once"})
        with self.assertRaisesRegex(rollout.RolloutRejected, "Conversation launch"):
            self.audit()

    def test_missing_directory_and_incomplete_project_inventory_rejected(self):
        (self.root / "mailboxes/project-5/results").rmdir()
        with self.assertRaisesRegex(rollout.RolloutRejected, "missing release evidence"):
            self.audit()
        self.mkdir(self.root / "mailboxes/project-5/results")
        with self.assertRaisesRegex(rollout.RolloutRejected, "project inventory"):
            inspect_disposable_mac_quiescence(
                self.root, project_ids=self.project_ids[:-1],
            )

    def test_stale_and_wrong_source_heartbeat_rejected(self):
        path = self.state / "status.json"
        payload = json.loads(path.read_text())
        payload["updated_at"] = "2020-01-01T00:00:00+00:00"
        self.write(path, payload)
        with self.assertRaisesRegex(rollout.RolloutRejected, "not fresh"):
            self.audit()
        payload["updated_at"] = dt.datetime.now(dt.timezone.utc).isoformat()
        payload["self_revision"] = "not-the-installed-base"
        self.write(path, payload)
        with self.assertRaisesRegex(rollout.RolloutRejected, "source/idle"):
            self.audit()

    def test_invalid_json_duplicate_and_symlink_result_rejected(self):
        target = self.root / "mailboxes/project-0/results/corrupt.json"
        target.write_text('{"status":"done","status":"unknown"}')
        target.chmod(0o600)
        with self.assertRaisesRegex(rollout.RolloutRejected, "duplicate"):
            self.audit()
        target.unlink()
        external = self.root / "outside"
        self.write(external, {"status": "done"})
        target.symlink_to(external)
        with self.assertRaisesRegex(rollout.RolloutRejected, "unsafe release ledger"):
            self.audit()

    def test_wrong_marker_or_orphan_config_blocks_even_if_queues_empty(self):
        (self.state / gate.FROZEN).write_text("{}")
        with self.assertRaisesRegex(rollout.RolloutRejected, "invalid durable"):
            self.audit()
        (self.state / gate.FROZEN).write_bytes(fence._FROZEN_DATA)
        (self.state / gate.FROZEN).chmod(0o600)
        (self.state / gate.CONFIG).unlink()
        with self.assertRaises(gate.MacReleaseAdmissionBlocked):
            self.audit()

    def test_exclusive_observation_cannot_race_active_worker(self):
        # Once frozen a newly admitted worker refuses immediately. Simulate
        # an unrelated exclusive holder to prove the inventory cannot run
        # without its own lock.
        hold = "\n".join([
            "import fcntl,os,sys",
            "fd=os.open(sys.argv[1],os.O_RDWR)",
            "fcntl.flock(fd,fcntl.LOCK_EX)",
            "print('ready',flush=True)",
            "sys.stdin.readline()",
        ])
        lock = self.state / gate.LOCK
        proc = subprocess.Popen(
            [sys.executable, "-c", hold, str(lock)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True,
        )
        try:
            self.assertEqual(proc.stdout.readline().strip(), "ready")
            with self.assertRaisesRegex(rollout.RolloutRejected, "Mac task is active"):
                self.audit()
        finally:
            proc.stdin.write("\n")
            proc.stdin.flush()
            proc.communicate(timeout=5)

    def test_private_files_and_unique_ids_required(self):
        target = self.root / "mailboxes/project-3/results/done.json"
        self.write(target, {"status": "done"})
        target.chmod(0o644)
        with self.assertRaisesRegex(rollout.RolloutRejected, "ownership/mode"):
            self.audit()
        target.chmod(0o600)
        with self.assertRaisesRegex(rollout.RolloutRejected, "invalid project inventory"):
            inspect_disposable_mac_quiescence(
                self.root, project_ids=("project-0", "project-0"),
            )


if __name__ == "__main__":
    unittest.main()
