"""Mac-only disposable evidence for post-GC orphan payload directories.

Never enumerate or delete a real Mac checkout. The replay/no-retry decision
cannot be derived from a residual directory.
"""
from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

from local_agent.repository.cleanup import (
    _rmdir_selected_empty_payload_dir,
    _selected_empty_payload_directories,
    control_cleanup_plan,
)
from local_agent.runtime.task_transport import write_task_bundle
from scripts.macos_installed_state_preflight import _count_unattached_task_payloads


@unittest.skipUnless(sys.platform == "darwin", "native macOS only")
class MacPayloadRetentionRehearsalTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="la-mac-payload-rehearsal-")
        self.addCleanup(self.temp.cleanup)
        self.control = Path(self.temp.name) / "control"
        self.control.mkdir(mode=0o700)
        self.tasks = self.control / ".agent/tasks"
        self.results = self.control / ".agent/results"
        self.results.mkdir(parents=True, mode=0o700)
        for node in (self.control / ".agent", self.tasks, self.results):
            node.mkdir(mode=0o700, exist_ok=True)
            node.chmod(0o700)

    def create_pair(self, identity="terminal-pair"):
        task = {
            "id": identity,
            "resources": [],
            "commands": ["printf '%s\\n' 'never execute in GC test'"],
            "command_timeout": 60,
            "task_timeout": 180,
        }
        write_task_bundle(self.tasks, task)
        (self.results / f"{identity}.json").write_text(json.dumps({
            "id": identity, "status": "done",
            "finished_at": "2026-09-28T10:00:00+00:00",
        }))
        return self.tasks / f"{identity}.payload"

    def test_terminal_pair_gc_removes_payload_files_but_retains_directories(self):
        root = self.create_pair()
        plan = control_cleanup_plan(
            self.control,
            terminal_pair_retention=0, run_retention=0,
            ack_retention=0, orphan_result_retention=0,
        )
        self.assertIn(".agent/tasks/terminal-pair.json", plan)
        self.assertIn(".agent/results/terminal-pair.json", plan)
        self.assertIn(".agent/tasks/terminal-pair.payload/commands/001.sh", plan)
        self.assertNotIn(".agent/tasks/terminal-pair.payload", plan)
        # Emulate ONLY the plans returned by source GC on disposable files.
        for relative in plan:
            (self.control / relative).unlink()
        self.assertTrue(root.is_dir())
        self.assertTrue((root / "commands").is_dir())
        self.assertFalse(any(path.is_file() for path in root.rglob("*")))
        counts = _count_unattached_task_payloads(self.tasks, base=self.control)
        self.assertEqual(counts["total"], 1)
        self.assertEqual(counts["directories_only"], 1)
        self.assertEqual(counts["files_present"], 0)

    def test_future_terminal_gc_removes_only_selected_empty_directories(self):
        root = self.create_pair("gc-fixed")
        plan = control_cleanup_plan(self.control, terminal_pair_retention=0)
        directories = _selected_empty_payload_directories(plan)
        self.assertIn(Path(".agent/tasks/gc-fixed.payload"),
                      [Path(str(p)) for p in directories])
        for relative in plan:
            (self.control / relative).unlink()
        removed = [_rmdir_selected_empty_payload_dir(self.control, p)
                   for p in directories]
        self.assertTrue(all(removed))
        self.assertFalse(root.exists())

    def test_future_terminal_gc_never_deletes_unlisted_payload_file(self):
        root = self.create_pair("kept-content")
        extra = root / "commands/not-listed.txt"
        extra.write_text("preserve untracked file", encoding="utf-8")
        plan = control_cleanup_plan(self.control, terminal_pair_retention=0)
        self.assertNotIn(".agent/tasks/kept-content.json", plan)
        self.assertNotIn(".agent/results/kept-content.json", plan)
        self.assertNotIn(".agent/tasks/kept-content.payload/commands/not-listed.txt", plan)
        self.assertEqual(_selected_empty_payload_directories(plan), ())
        self.assertTrue(extra.exists())
        self.assertEqual(extra.read_text(), "preserve untracked file")
        self.assertTrue((self.tasks / "kept-content.json").is_file())
        self.assertTrue((self.results / "kept-content.json").is_file())

    def test_symlinked_payload_is_not_selected_by_terminal_gc(self):
        self.create_pair("symlink-payload")
        root = self.tasks / "symlink-payload.payload"
        target = self.control / "external-sentinel"
        target.mkdir(mode=0o700)
        root.rename(self.control / "reserved-real-bundle")
        root.symlink_to(target, target_is_directory=True)
        plan = control_cleanup_plan(self.control, terminal_pair_retention=0)
        self.assertNotIn(".agent/tasks/symlink-payload.json", plan)
        self.assertNotIn(".agent/results/symlink-payload.json", plan)
        self.assertTrue(root.is_symlink())
        self.assertTrue(target.is_dir())

    def test_no_unselected_orphan_will_enter_directory_pruning_plan(self):
        root = self.tasks / "unrelated.payload"
        root.mkdir(mode=0o700)
        self.assertEqual(_selected_empty_payload_directories(()), ())
        self.assertTrue(root.is_dir())

    def test_symlinked_parent_is_never_traversed_for_empty_directory_removal(self):
        root = self.create_pair("no-redirect")
        plan = control_cleanup_plan(self.control, terminal_pair_retention=0)
        for relative in plan:
            (self.control / relative).unlink()
        control_agent = self.control / ".agent"
        relocated = self.control / "relocated-agent"
        control_agent.rename(relocated)
        control_agent.symlink_to(relocated, target_is_directory=True)
        with self.assertRaises(OSError):
            for directory in _selected_empty_payload_directories(plan):
                _rmdir_selected_empty_payload_dir(self.control, directory)
        self.assertTrue((relocated / "tasks/no-redirect.payload").is_dir())
        self.assertFalse((root / "commands").is_symlink())

    def test_failed_manifest_publication_cleans_local_payload_reservation(self):
        task = {
            "id": "failure-rollback",
            "resources": [],
            "commands": ["true"],
            "command_timeout": 60,
            "task_timeout": 180,
        }
        from local_agent.runtime import task_transport

        with mock.patch.object(
            task_transport, "_atomic_write_text_no_clobber",
            side_effect=RuntimeError("simulate before publish"),
        ):
            with self.assertRaisesRegex(RuntimeError, "simulate before publish"):
                write_task_bundle(self.tasks, task)
        self.assertFalse((self.tasks / "failure-rollback.payload").exists())
        self.assertFalse((self.tasks / "failure-rollback.json").exists())


if __name__ == "__main__":
    unittest.main()
