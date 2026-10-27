"""Offline, fail-closed attached snapshot inventory regressions."""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from local_agent.repository import snapshot_review


class SnapshotReviewTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory(prefix="la-review-")
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name)
        self.checkpoints = self.base / "checkpoints"
        self.root = self.checkpoints / "attached-task-workspaces"
        self.root.mkdir(parents=True)
        self.task = self.root / "task-abc"
        (self.task / "work").mkdir(parents=True)
        self.source = {
            "mode": "attached_isolated", "task_id": "task-id",
            "fingerprint": "a" * 64, "source_head": "b" * 40,
        }
        self.completion = {
            "version": 1, "state": "completed_with_changes",
            "task_id": "task-id", "source_fingerprint": "a" * 64,
            "result_fingerprint": "c" * 64, "promotion": "manual_only",
            "owner_reconciliation": "not_checked",
        }
        self.write("snapshot.json", self.source)

    def write(self, name: str, payload: dict) -> None:
        (self.task / name).write_text(json.dumps(payload))

    def review(self, limit: int = 25) -> dict:
        return snapshot_review.inspect_attached_snapshots(self.checkpoints, limit=limit)

    def record(self) -> dict:
        return self.review()["snapshots"][0]

    def test_interrupted_and_completed_are_never_promotable(self) -> None:
        self.assertEqual(self.record()["review_state"], "interrupted_or_unfinished")
        self.write("completion.json", self.completion)
        record = self.record()
        self.assertEqual(record["review_state"], "completed_with_changes")
        self.assertFalse(record["promotion_allowed"])
        self.assertFalse(self.review()["worktree_contents_read"])

    def test_completion_mismatch_and_untrusted_promotion_fail_closed(self) -> None:
        for field, value in (("task_id", "wrong"), ("promotion", "automatic"),
                             ("owner_reconciliation", "approved"), ("version", 2)):
            with self.subTest(field=field):
                self.write("completion.json", {**self.completion, field: value})
                self.assertEqual(self.record()["reason"], "untrusted_or_invalid_manifest")

    def test_symlinked_manifest_and_dangling_completion_never_followed(self) -> None:
        external = self.base / "private.json"
        external.write_text(json.dumps({**self.source, "source_head": "e" * 40}))
        (self.task / "snapshot.json").unlink()
        (self.task / "snapshot.json").symlink_to(external)
        self.assertEqual(self.record()["reason"], "untrusted_or_invalid_manifest")
        self.assertNotIn("source_head", self.record())
        (self.task / "snapshot.json").unlink()
        self.write("snapshot.json", self.source)
        (self.task / "completion.json").symlink_to(self.base / "absent")
        self.assertEqual(self.record()["reason"], "untrusted_or_invalid_manifest")

    def test_fifo_and_oversize_manifest_fail_without_hanging(self) -> None:
        (self.task / "snapshot.json").unlink()
        os.mkfifo(self.task / "snapshot.json")
        self.assertEqual(self.record()["reason"], "untrusted_or_invalid_manifest")
        (self.task / "snapshot.json").unlink()
        (self.task / "snapshot.json").write_bytes(b"x" * 16385)
        self.assertEqual(self.record()["reason"], "untrusted_or_invalid_manifest")

    def test_symlinked_root_work_and_task_fail_closed(self) -> None:
        self.checkpoints.rename(self.base / "old")
        self.checkpoints.symlink_to(self.base / "old", target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "unsafe"):
            self.review()
        self.checkpoints.unlink()
        (self.base / "old").rename(self.checkpoints)
        (self.task / "work").rename(self.task / "real-work")
        (self.task / "work").symlink_to(self.task / "real-work", target_is_directory=True)
        self.assertEqual(self.record()["reason"], "isolated_worktree_missing_or_unsafe")
        (self.task / "work").unlink()
        (self.task / "real-work").rename(self.task / "work")
        self.task.rename(self.root / "real-task")
        self.task.symlink_to(self.root / "real-task", target_is_directory=True)
        self.assertEqual(self.record()["reason"], "unexpected_snapshot_entry")

    def test_directory_swap_during_listing_does_not_follow_redirect(self) -> None:
        external = self.base / "external"
        external.mkdir()
        (external / "work").mkdir()
        (external / "snapshot.json").write_text(json.dumps({**self.source, "source_head": "e" * 40}))
        original = snapshot_review._record

        def swap(root: int, name: str, root_path: Path) -> dict:
            self.task.rename(self.root / "task-original")
            self.task.symlink_to(external, target_is_directory=True)
            return original(root, name, root_path)

        with mock.patch.object(snapshot_review, "_record", side_effect=swap):
            record = self.record()
        self.assertEqual(record["reason"], "unexpected_snapshot_entry")
        self.assertNotIn("source_head", record)

    def test_bounded_listing_missing_root_and_bad_limit(self) -> None:
        for limit in (0, 101, True, 1.5):
            with self.subTest(limit=limit), self.assertRaises(ValueError):
                self.review(limit)
        (self.root / "task-other").mkdir()
        result = self.review(limit=1)
        self.assertEqual(result["returned"], 1)
        self.assertTrue(result["truncated"])
        self.checkpoints.rename(self.base / "gone")
        self.assertEqual(self.review()["returned"], 0)


    def test_targeted_snapshot_lookup_beyond_list_limit_and_strict_id(self) -> None:
        for index in range(6):
            (self.root / f"task-extra-{index}").mkdir()
        direct = self.review()
        self.assertGreater(direct["returned"], 1)
        targeted = snapshot_review.inspect_attached_snapshots(
            self.checkpoints, limit=1, snapshot_id="task-abc",
        )
        self.assertEqual(targeted["returned"], 1)
        self.assertFalse(targeted["truncated"])
        self.assertEqual(targeted["snapshots"][0]["task_id"], "task-id")
        missing = snapshot_review.inspect_attached_snapshots(
            self.checkpoints, limit=1, snapshot_id="task-does-not-exist",
        )
        self.assertEqual(missing["snapshots"], [])
        for bad in ("../task-abc", "", "task-", "task-/../../etc", "other-abc", None):
            if bad is None:
                continue
            with self.subTest(bad=bad), self.assertRaisesRegex(ValueError, "snapshot id"):
                snapshot_review.inspect_attached_snapshots(
                    self.checkpoints, snapshot_id=bad,
                )
        outside = self.base / "external-target"
        outside.mkdir()
        (self.root / "task-redirect").symlink_to(outside, target_is_directory=True)
        redirected = snapshot_review.inspect_attached_snapshots(
            self.checkpoints, snapshot_id="task-redirect",
        )
        self.assertEqual(redirected["returned"], 1)
        self.assertEqual(redirected["snapshots"][0]["reason"], "unexpected_snapshot_entry")
        self.assertNotIn("task_id", redirected["snapshots"][0])


    def test_symlinked_checkpoint_parent_is_refused(self) -> None:
        alias = self.base / "redirect"
        alias.symlink_to(self.base, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "unsafe"):
            snapshot_review.inspect_attached_snapshots(alias / "checkpoints")
        with self.assertRaisesRegex(ValueError, "parent traversal"):
            snapshot_review.inspect_attached_snapshots(
                self.base / "child" / ".." / "checkpoints",
            )

    def test_parent_swap_after_open_remains_on_pinned_inode(self) -> None:
        parent = self.base / "parent"
        parent.mkdir()
        self.checkpoints.rename(parent / "checkpoints")
        self.checkpoints = parent / "checkpoints"
        self.root = self.checkpoints / "attached-task-workspaces"
        self.task = self.root / "task-abc"
        outside = self.base / "outside"
        external = outside / "checkpoints" / "attached-task-workspaces" / "task-abc"
        (external / "work").mkdir(parents=True)
        (external / "snapshot.json").write_text(
            json.dumps({**self.source, "source_head": "e" * 40}),
        )
        real_open = os.open
        swapped = False

        def swap_after_open(name: str, flags: int, *, dir_fd: int | None = None) -> int:
            nonlocal swapped
            fd = real_open(name, flags, dir_fd=dir_fd)
            if name == "parent" and not swapped:
                swapped = True
                parent.rename(self.base / "pinned-parent")
                parent.symlink_to(outside, target_is_directory=True)
            return fd

        with mock.patch.object(snapshot_review.os, "open", side_effect=swap_after_open):
            record = snapshot_review.inspect_attached_snapshots(
                self.checkpoints, snapshot_id="task-abc",
            )["snapshots"][0]
        self.assertTrue(swapped)
        self.assertEqual(record["source_head"], "b" * 40)
        self.assertNotEqual(record["source_head"], "e" * 40)
        self.assertFalse(record["promotion_allowed"])


if __name__ == "__main__":
    unittest.main()
