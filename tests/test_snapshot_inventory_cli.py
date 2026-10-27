"""Read-only CLI coverage for retained attached-task snapshot inventory."""
from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from local_agent.repository import admin


class SnapshotInventoryCliTests(unittest.TestCase):
    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory(prefix="la-snapshot-cli-")
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name)
        self.registry = self.base / "repositories.json"
        self.work = self.base / "owner-dirty"
        self.work.mkdir()
        (self.work / "keep.txt").write_text("OWNER CONTENT")
        self.checkpoints = self.base / "checkpoints"
        self.root = self.checkpoints / "attached-task-workspaces"
        self.root.mkdir(parents=True)
        self.source = {
            "mode": "attached_isolated", "task_id": "fixture",
            "fingerprint": "a" * 64, "source_head": "b" * 40,
        }
        self.entry = {
            "id": "one", "repository": "owner/one", "enabled": False,
            "work_mode": "attached",
            "work_dir": str(self.work),
            "control_dir": str(self.base / "control"),
            "checkpoints_dir": str(self.checkpoints),
        }
        self.write_registry(self.entry)
        task = self.root / "task-abc"
        (task / "work").mkdir(parents=True)
        (task / "snapshot.json").write_text(json.dumps(self.source))

    def write_registry(self, entry: dict) -> None:
        self.registry.write_text(json.dumps({
            "version": 1, "repositories": [entry],
        }) + "\n")

    def cli(self, *args: str) -> tuple[int, dict]:
        stdout = io.StringIO()
        argv = [
            "admin.py", "--registry", str(self.registry),
            "snapshots", "--repository-id", "one", *args,
        ]
        with mock.patch("sys.argv", argv), contextlib.redirect_stdout(stdout):
            rc = admin.main()
        return rc, json.loads(stdout.getvalue())

    def test_disabled_attached_project_inventory_is_read_only(self) -> None:
        registry_before = self.registry.read_bytes()
        owner_before = (self.work / "keep.txt").read_bytes()
        snapshot_before = (self.root / "task-abc" / "snapshot.json").read_bytes()
        with mock.patch.object(admin, "run_git", side_effect=AssertionError("Git forbidden")), \
             mock.patch.object(admin, "provision_repository", side_effect=AssertionError("provision forbidden")):
            rc, result = self.cli()
        self.assertEqual(rc, 0)
        self.assertEqual(result["status"], "snapshot_inventory_only")
        self.assertEqual(result["repository_id"], "one")
        self.assertFalse(result["repository_enabled"])
        self.assertEqual(result["returned"], 1)
        self.assertEqual(result["snapshots"][0]["review_state"], "interrupted_or_unfinished")
        self.assertFalse(result["promotion_allowed"])
        self.assertFalse(result["worktree_contents_read"])
        self.assertFalse(result["changes_applied"])
        self.assertEqual(result["execution_authority"], "not_asserted")
        self.assertEqual(self.registry.read_bytes(), registry_before)
        self.assertEqual((self.work / "keep.txt").read_bytes(), owner_before)
        self.assertEqual((self.root / "task-abc" / "snapshot.json").read_bytes(), snapshot_before)

    def test_completed_work_cannot_trigger_promotion(self) -> None:
        task = self.root / "task-abc"
        completion = {
            "version": 1, "state": "completed_with_changes",
            "task_id": "fixture", "source_fingerprint": "a" * 64,
            "result_fingerprint": "c" * 64, "promotion": "manual_only",
            "owner_reconciliation": "not_checked",
        }
        (task / "completion.json").write_text(json.dumps(completion))
        rc, result = self.cli()
        self.assertEqual(rc, 0)
        self.assertEqual(result["snapshots"][0]["review_state"], "completed_with_changes")
        self.assertFalse(result["promotion_allowed"])
        self.assertEqual((self.work / "keep.txt").read_text(), "OWNER CONTENT")

    def test_managed_repository_and_missing_id_fail_closed(self) -> None:
        self.write_registry({**self.entry, "work_mode": "managed"})
        rc, result = self.cli()
        self.assertEqual(rc, 2)
        self.assertIn("requires an attached", result["error"])
        self.write_registry({**self.entry, "id": "different"})
        rc, result = self.cli()
        self.assertEqual(rc, 2)
        self.assertIn("not registered", result["error"])

    def test_bad_limit_symlink_and_corrupt_registry_fail_closed(self) -> None:
        for limit in ("0", "101", "-1"):
            with self.subTest(limit=limit):
                rc, result = self.cli("--limit", limit)
                self.assertEqual(rc, 2)
                self.assertFalse(result["changes_applied"])
        redirected = self.base / "redirected"
        self.checkpoints.rename(redirected)
        self.checkpoints.symlink_to(redirected, target_is_directory=True)
        rc, result = self.cli()
        self.assertEqual(rc, 2)
        self.assertEqual(result["status"], "unverified")
        self.checkpoints.unlink()
        redirected.rename(self.checkpoints)
        self.registry.write_text("{broken")
        rc, result = self.cli()
        self.assertEqual(rc, 2)
        self.assertEqual(result["status"], "unverified")

    def test_zero_snapshot_directory_and_bounded_listing(self) -> None:
        (self.root / "task-abc" / "snapshot.json").unlink()
        (self.root / "task-abc").rename(self.root / "task-zzz")
        (self.root / "task-zzz" / "work").rmdir()
        (self.root / "task-zzz").rmdir()
        rc, result = self.cli()
        self.assertEqual(rc, 0)
        self.assertEqual(result["snapshots"], [])
        for name in ("task-a", "task-b"):
            (self.root / name).mkdir()
        rc, result = self.cli("--limit", "1")
        self.assertEqual(rc, 0)
        self.assertTrue(result["truncated"])
        self.assertEqual(result["returned"], 1)


    def test_targeted_snapshot_cli_bypasses_bounded_listing(self) -> None:
        for index in range(8):
            (self.root / f"task-fill-{index}").mkdir()
        rc, result = self.cli("--limit", "1", "--snapshot-id", "task-abc")
        self.assertEqual(rc, 0)
        self.assertEqual(result["returned"], 1)
        self.assertFalse(result["truncated"])
        self.assertEqual(result["snapshots"][0]["task_id"], "fixture")
        rc, result = self.cli("--snapshot-id", "task-no-such-result")
        self.assertEqual(rc, 0)
        self.assertEqual(result["snapshots"], [])
        rc, result = self.cli("--snapshot-id", "../escape")
        self.assertEqual(rc, 2)
        self.assertEqual(result["status"], "unverified")
        self.assertFalse(result["changes_applied"])

    def test_explicit_completed_snapshot_comparison_is_path_only(self) -> None:
        first = self.root / "task-abc"
        second = self.root / "task-other"
        (second / "work").mkdir(parents=True)
        (first / "work" / "notes.txt").write_text("alpha")
        (second / "work" / "notes.txt").write_text("bravo")
        (second / "snapshot.json").write_text(json.dumps(self.source))
        completion = {
            "version": 1, "state": "completed_with_changes",
            "task_id": "fixture", "source_fingerprint": "a" * 64,
            "result_fingerprint": "c" * 64, "promotion": "manual_only",
            "owner_reconciliation": "not_checked",
        }
        for task in (first, second):
            (task / "completion.json").write_text(json.dumps(completion))
        registry_before = self.registry.read_bytes()
        owner_before = (self.work / "keep.txt").read_bytes()
        with mock.patch.object(admin, "run_git", side_effect=AssertionError("Git forbidden")):
            rc, result = self.cli(
                "--snapshot-id", "task-abc", "--compare-snapshot-id", "task-other",
            )
        self.assertEqual(rc, 0)
        self.assertEqual(result["status"], "snapshot_comparison_unverified")
        self.assertEqual(result["changed_paths"], [
            {"path": "notes.txt", "change": "modified"},
        ])
        self.assertFalse(result["promotion_allowed"])
        self.assertFalse(result["owner_checkout_read"])
        self.assertFalse(result["changes_applied"])
        self.assertEqual(self.registry.read_bytes(), registry_before)
        self.assertEqual((self.work / "keep.txt").read_bytes(), owner_before)
        rc, invalid = self.cli("--compare-snapshot-id", "task-other")
        self.assertEqual(rc, 2)
        self.assertEqual(invalid["status"], "unverified")



if __name__ == "__main__":
    unittest.main()
