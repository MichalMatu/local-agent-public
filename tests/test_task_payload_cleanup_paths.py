from __future__ import annotations

import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from local_agent.repository import cleanup


class TaskPayloadCleanupPathTests(unittest.TestCase):
    def test_cleanup_plan_uses_filename_for_noncanonical_embedded_task_id(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            control = Path(temporary)
            tasks_dir = control / ".agent/tasks"
            results_dir = control / ".agent/results"
            tasks_dir.mkdir(parents=True)
            results_dir.mkdir(parents=True)
            (control / ".agent/binding.json").write_text(
                '{"version":1}\n',
                encoding="utf-8",
            )
            (tasks_dir / "safe-name.json").write_text(
                '{"id":"../binding"}\n',
                encoding="utf-8",
            )
            (results_dir / "safe-name.json").write_text(
                '{"finished_at":"2026-09-28T10:00:00+00:00"}\n',
                encoding="utf-8",
            )

            plan = cleanup.control_cleanup_plan(
                control,
                terminal_pair_retention=0,
                run_retention=0,
                ack_retention=0,
                orphan_result_retention=0,
            )

            self.assertIn(".agent/tasks/safe-name.json", plan)
            self.assertIn(".agent/results/safe-name.json", plan)
            self.assertNotIn(".agent/binding.json", plan)
            self.assertTrue(all(".." not in Path(path).parts for path in plan))

    def test_prune_rejects_parent_traversal_before_unlink(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            control = Path(temporary)
            (control / ".git").mkdir()
            target = control / ".agent/binding.json"
            target.parent.mkdir(parents=True)
            target.write_text('{"version":1}\n', encoding="utf-8")

            class FakeCore:
                CONTROL = control
                CONTROL_BRANCH = "agent-control"
                CONTROL_GIT_LOCK = threading.Lock()
                ENV: dict[str, str] = {}

                @staticmethod
                def process(*_args, **_kwargs):
                    return {"exit_code": 0, "output": ""}

            with mock.patch.object(
                cleanup,
                "control_cleanup_plan",
                return_value=(".agent/tasks/../binding.json",),
            ), mock.patch.object(
                cleanup,
                "_compact_control_history_locked",
                return_value={"changed": False, "reason": "below_threshold"},
            ):
                with self.assertRaisesRegex(ValueError, "non-canonical"):
                    cleanup.prune_control_runtime(FakeCore)

            self.assertTrue(target.is_file())
            self.assertEqual(target.read_text(encoding="utf-8"), '{"version":1}\n')


if __name__ == "__main__":
    unittest.main()
