"""Offline negative and positive coverage for advisory private-snapshot diff."""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from local_agent.repository import snapshot_compare


class SnapshotCompareTests(unittest.TestCase):
    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory(prefix="la-snapshot-compare-")
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name)
        self.checkpoints = self.base / "checkpoints"
        self.root = self.checkpoints / "attached-task-workspaces"
        self.left = self.root / "task-left"
        self.right = self.root / "task-right"
        self.start = {
            "mode": "attached_isolated", "task_id": "source-task",
            "fingerprint": "a" * 64, "source_head": "b" * 40,
        }
        self.done = {
            "version": 1, "state": "completed_with_changes",
            "task_id": "source-task", "source_fingerprint": "a" * 64,
            "result_fingerprint": "c" * 64, "promotion": "manual_only",
            "owner_reconciliation": "not_checked",
        }
        for path in (self.left, self.right):
            (path / "work" / "nested").mkdir(parents=True)
            (path / "snapshot.json").write_text(json.dumps(self.start))
            (path / "completion.json").write_text(json.dumps(self.done))
        (self.left / "work" / "nested" / "config.txt").write_text("old")
        (self.right / "work" / "nested" / "config.txt").write_text("new")
        (self.right / "work" / "added.txt").write_text("added")
        # Hidden Git metadata should never be read or interpreted.
        (self.left / "work" / ".git").mkdir()
        (self.left / "work" / ".git" / "config").write_text("unsafe configuration")

    def compare(self) -> dict:
        return snapshot_compare.compare_attached_snapshots(
            self.checkpoints, "task-left", "task-right",
        )

    def test_changed_paths_only_and_no_owner_files(self) -> None:
        owner = self.base / "owner"
        owner.mkdir()
        original = owner / "config.txt"
        original.write_text("keep owner")
        result = self.compare()
        self.assertEqual(result["status"], "snapshot_comparison_unverified")
        self.assertEqual(result["changed_paths"], [
            {"path": "added.txt", "change": "added"},
            {"path": "nested/config.txt", "change": "modified"},
        ])
        self.assertFalse(result["content_returned"])
        self.assertFalse(result["owner_checkout_read"])
        self.assertFalse(result["owner_checkout_modified"])
        self.assertFalse(result["promotion_allowed"])
        self.assertEqual(original.read_text(), "keep owner")
        self.assertNotIn("unsafe configuration", json.dumps(result))
        self.assertNotIn("old", json.dumps(result))

    def test_removed_and_same_size_changed_files_are_detected(self) -> None:
        (self.left / "work" / "removed.txt").write_text("gone")
        changes = self.compare()["changed_paths"]
        self.assertIn({"path": "removed.txt", "change": "removed"}, changes)
        self.assertIn({"path": "nested/config.txt", "change": "modified"}, changes)

    def test_requires_same_provenance_and_valid_completion(self) -> None:
        for field, value in (("fingerprint", "d" * 64), ("source_head", "e" * 40)):
            with self.subTest(field=field):
                (self.right / "snapshot.json").write_text(
                    json.dumps({**self.start, field: value}),
                )
                with self.assertRaises(ValueError):
                    self.compare()
                (self.right / "snapshot.json").write_text(json.dumps(self.start))
        for name in ("completion.json", "snapshot.json"):
            path = self.right / name
            path.unlink()
            with self.assertRaises(ValueError):
                self.compare()
            path.write_text(json.dumps(self.done if name.startswith("completion") else self.start))
        (self.right / "completion.json").write_text(
            json.dumps({**self.done, "promotion": "automatic"}),
        )
        with self.assertRaises(ValueError):
            self.compare()

    def test_symlinked_root_parent_and_snapshot_data_fail_closed(self) -> None:
        link = self.base / "redirect"
        link.symlink_to(self.base, target_is_directory=True)
        with self.assertRaises(ValueError):
            snapshot_compare.compare_attached_snapshots(
                link / "checkpoints", "task-left", "task-right",
            )
        symlink = self.right / "work" / "link"
        symlink.symlink_to(self.base)
        with self.assertRaises(ValueError):
            self.compare()
        symlink.unlink()
        (self.right / "snapshot.json").unlink()
        (self.right / "snapshot.json").symlink_to(self.left / "snapshot.json")
        with self.assertRaises(ValueError):
            self.compare()

    def test_unsafe_special_files_and_budgets_fail_closed(self) -> None:
        fifo = self.right / "work" / "fifo"
        os.mkfifo(fifo)
        with self.assertRaises(ValueError):
            self.compare()
        fifo.unlink()
        big = self.right / "work" / "big.dat"
        big.write_bytes(b"x" * (512 * 1024 + 1))
        with self.assertRaises(ValueError):
            self.compare()
        big.unlink()
        for i in range(130):
            (self.right / "work" / f"file-{i}").write_text("x")
        with self.assertRaises(ValueError):
            self.compare()

    def test_rejects_bad_ids_and_same_snapshot(self) -> None:
        for first, second in (("../bad", "task-right"), ("task-left", "../bad"),
                              ("task-left", "task-left")):
            with self.subTest(first=first, second=second), self.assertRaises(ValueError):
                snapshot_compare.compare_attached_snapshots(
                    self.checkpoints, first, second,
                )

    def test_snapshot_directory_swap_after_open_remains_pinned(self) -> None:
        outside = self.base / "outside"
        (outside / "work").mkdir(parents=True)
        (outside / "work" / "poison.txt").write_text("poison")
        (outside / "snapshot.json").write_text(json.dumps(self.start))
        (outside / "completion.json").write_text(json.dumps(self.done))
        real_open = os.open
        swapped = False

        def swap(name: str, flags: int, *, dir_fd: int | None = None) -> int:
            nonlocal swapped
            fd = real_open(name, flags, dir_fd=dir_fd)
            if name == "task-left" and not swapped:
                swapped = True
                self.left.rename(self.root / "task-pinned")
                self.left.symlink_to(outside, target_is_directory=True)
            return fd

        with mock.patch.object(snapshot_compare.os, "open", side_effect=swap):
            result = self.compare()
        self.assertTrue(swapped)
        self.assertEqual(result["changed_count"], 2)
        self.assertNotIn("poison.txt", [x["path"] for x in result["changed_paths"]])


    def test_directory_entry_created_during_scan_fails_closed(self) -> None:
        original_read = snapshot_compare._read_file
        injected = False

        def create_late_entry(parent: int, name: str, budget: dict[str, int]) -> tuple[int, str]:
            nonlocal injected
            if name == "config.txt" and not injected:
                injected = True
                (self.left / "work" / "nested" / "raced.txt").write_text("late")
            return original_read(parent, name, budget)

        with mock.patch.object(snapshot_compare, "_read_file", side_effect=create_late_entry):
            with self.assertRaisesRegex(ValueError, "directory changed"):
                self.compare()
        self.assertTrue(injected)

    def test_file_changed_after_first_pass_fails_closed(self) -> None:
        original_collect = snapshot_compare._collect
        injected = False

        def change_after_first_pass(
            directory: int, prefix: str, depth: int,
            budget: dict[str, int], files: dict[str, tuple[int, str]],
        ) -> None:
            nonlocal injected
            original_collect(directory, prefix, depth, budget, files)
            if prefix == "" and not injected:
                injected = True
                # Rewriting an existing file need not update its parent
                # directory metadata. The second digest pass must catch it.
                (self.left / "work" / "nested" / "config.txt").write_text("late edit")

        with mock.patch.object(snapshot_compare, "_collect", side_effect=change_after_first_pass):
            with self.assertRaisesRegex(ValueError, "between comparison passes"):
                self.compare()
        self.assertTrue(injected)

    def test_unchanged_tree_reports_advisory_double_read(self) -> None:
        result = self.compare()
        self.assertEqual(result["limits"]["advisory_recheck_passes"], 2)
        self.assertEqual(result["changed_count"], 2)
        self.assertFalse(result["promotion_allowed"])


if __name__ == "__main__":
    unittest.main()
