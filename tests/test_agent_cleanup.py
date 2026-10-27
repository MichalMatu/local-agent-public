from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import local_agent.repository.cleanup as cleanup


def write_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload) + "\n", encoding="utf-8")


class ControlCleanupPlanTests(unittest.TestCase):
    def test_prunes_old_terminal_pairs_but_never_pending_tasks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            control = Path(tmp)
            for index in range(6):
                task_id = f"task-{index}"
                write_json(control / ".agent/tasks" / f"queue-{index}.json", {"id": task_id})
                write_json(
                    control / ".agent/results" / f"{task_id}.json",
                    {"updated_at": f"2026-09-03T00:00:0{index}+00:00"},
                )
            write_json(control / ".agent/tasks/pending.json", {"id": "pending-task"})

            plan = cleanup.control_cleanup_plan(
                control,
                terminal_pair_retention=2,
                run_retention=32,
                ack_retention=16,
                orphan_result_retention=8,
            )

            self.assertIn(".agent/tasks/queue-0.json", plan)
            self.assertIn(".agent/results/task-0.json", plan)
            self.assertIn(".agent/tasks/queue-3.json", plan)
            self.assertNotIn(".agent/tasks/queue-4.json", plan)
            self.assertNotIn(".agent/tasks/queue-5.json", plan)
            self.assertNotIn(".agent/tasks/pending.json", plan)

    def test_pending_task_run_is_protected_from_run_retention(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            control = Path(tmp)
            write_json(control / ".agent/tasks/pending.json", {"id": "pending-task"})
            write_json(
                control / ".agent/runs/pending-task.json",
                {"updated_at": "2020-01-01T00:00:00+00:00"},
            )
            for index in range(4):
                write_json(
                    control / ".agent/runs" / f"done-{index}.json",
                    {"updated_at": f"2026-09-03T00:00:0{index}+00:00"},
                )

            plan = cleanup.control_cleanup_plan(
                control,
                terminal_pair_retention=32,
                run_retention=1,
                ack_retention=16,
                orphan_result_retention=8,
            )

            self.assertNotIn(".agent/runs/pending-task.json", plan)
            self.assertNotIn(".agent/runs/done-3.json", plan)
            self.assertIn(".agent/runs/done-0.json", plan)
            self.assertIn(".agent/runs/done-2.json", plan)

    def test_ack_for_current_control_request_is_always_protected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            control = Path(tmp)
            write_json(
                control / ".agent/daemon/control.json",
                {"id": "current", "action": "status"},
            )
            write_json(
                control / ".agent/daemon/acks/current.json",
                {"updated_at": "2020-01-01T00:00:00+00:00"},
            )
            for index in range(3):
                write_json(
                    control / ".agent/daemon/acks" / f"ack-{index}.json",
                    {"updated_at": f"2026-09-03T00:00:0{index}+00:00"},
                )

            plan = cleanup.control_cleanup_plan(
                control,
                terminal_pair_retention=32,
                run_retention=32,
                ack_retention=1,
                orphan_result_retention=8,
            )

            self.assertNotIn(".agent/daemon/acks/current.json", plan)
            self.assertNotIn(".agent/daemon/acks/ack-2.json", plan)
            self.assertIn(".agent/daemon/acks/ack-0.json", plan)
            self.assertIn(".agent/daemon/acks/ack-1.json", plan)

    def test_orphan_results_are_bounded(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            control = Path(tmp)
            for index in range(4):
                write_json(
                    control / ".agent/results" / f"orphan-{index}.json",
                    {"updated_at": f"2026-09-03T00:00:0{index}+00:00"},
                )

            plan = cleanup.control_cleanup_plan(
                control,
                terminal_pair_retention=32,
                run_retention=32,
                ack_retention=16,
                orphan_result_retention=1,
            )

            self.assertNotIn(".agent/results/orphan-3.json", plan)
            self.assertIn(".agent/results/orphan-0.json", plan)
            self.assertIn(".agent/results/orphan-2.json", plan)

    def test_negative_retention_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ValueError):
                cleanup.control_cleanup_plan(Path(tmp), terminal_pair_retention=-1)


if __name__ == "__main__":
    unittest.main()
