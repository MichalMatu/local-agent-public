from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from local_agent.runtime.task_contract import task_digest
from local_agent.supervisor import task_dedupe


class TaskDedupeTests(unittest.TestCase):
    def task(self, task_id: str, command: str = "true") -> dict[str, object]:
        return {
            "id": task_id,
            "agent_binding": "3da0947d-9acf-4ecf-adce-a29be7dc5c09",
            "mode": "commands",
            "work_branch": "main",
            "allow_write": True,
            "resources": [],
            "task_timeout": 1800,
            "steps": [
                {
                    "name": f"stage-{task_id}",
                    "command": command,
                    "timeout": 120,
                    "output_policy": "summary",
                }
            ],
        }

    def create_claim(self, state_dir: Path, task_id: str) -> None:
        path = task_dedupe._claim_path(state_dir, task_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{}\n", encoding="utf-8")

    def test_execution_fingerprint_ignores_identity_and_cosmetic_stage_metadata(self) -> None:
        first = self.task("task-a")
        second = self.task("task-b")
        second["output_policy"] = "stream"
        second["steps"] = [
            {
                "name": "renamed-stage",
                "command": "true",
                "timeout": 120,
                "output_policy": "stream",
            }
        ]

        self.assertEqual(
            task_dedupe.execution_fingerprint(first),
            task_dedupe.execution_fingerprint(second),
        )

    def test_execution_fingerprint_changes_when_completion_limits_change(self) -> None:
        baseline = self.task("task-a")
        variants = []

        command_timeout = self.task("task-b")
        command_timeout["command_timeout"] = 600
        variants.append(command_timeout)

        stage_timeout = self.task("task-c")
        stage_timeout["steps"] = [
            {
                "name": "stage-task-c",
                "command": "true",
                "timeout": 30,
                "output_policy": "summary",
            }
        ]
        variants.append(stage_timeout)

        task_timeout = self.task("task-d")
        task_timeout["task_timeout"] = 2400
        variants.append(task_timeout)

        idle_timeout = self.task("task-e")
        idle_timeout["idle_timeout"] = 60
        variants.append(idle_timeout)

        memory_limit = self.task("task-f")
        memory_limit["memory_limit_mb"] = 1024
        variants.append(memory_limit)

        for variant in variants:
            with self.subTest(task_id=variant["id"]):
                self.assertNotEqual(
                    task_dedupe.execution_fingerprint(baseline),
                    task_dedupe.execution_fingerprint(variant),
                )

    def test_execution_fingerprint_changes_when_effect_changes(self) -> None:
        self.assertNotEqual(
            task_dedupe.execution_fingerprint(self.task("task-a", "true")),
            task_dedupe.execution_fingerprint(self.task("task-b", "false")),
        )

    def test_explicit_dedupe_key_is_canonical_stable_and_branch_scoped(self) -> None:
        first = self.task("task-a", "first")
        second = self.task("task-b", "second")
        first["dedupe_key"] = "example-project:telegram:worker-quality"
        second["dedupe_key"] = "example-project:telegram:worker-quality"

        self.assertEqual(task_dedupe.queue_key(first), task_dedupe.queue_key(second))
        second["work_branch"] = "release"
        self.assertNotEqual(task_dedupe.queue_key(first), task_dedupe.queue_key(second))

        with self.assertRaisesRegex(ValueError, "canonical"):
            task_dedupe.queue_key({**first, "dedupe_key": " bad "})
        with self.assertRaisesRegex(ValueError, "unsupported"):
            task_dedupe.queue_key({**first, "dedupe_key": "bad key"})

    def test_plan_suppresses_same_effect_with_different_task_ids(self) -> None:
        pending = [
            (Path("a.json"), self.task("task-a")),
            (Path("b.json"), self.task("task-b")),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            plan = task_dedupe.plan_pending(Path(tmp), pending, now_epoch=100.0)

        self.assertEqual([item[1]["id"] for item in plan.candidates], ["task-a"])
        self.assertEqual(len(plan.suppressed), 1)
        self.assertEqual(plan.suppressed[0].duplicate_of, "task-a")
        self.assertEqual(plan.suppressed[0].reason, "queued_duplicate")
        self.assertEqual(plan.invalid, ())

    def test_published_timeout_failure_does_not_suppress_corrected_retry(self) -> None:
        first = self.task("task-a", "long-running-command")
        first["steps"] = [
            {
                "name": "attempt",
                "command": "long-running-command",
                "timeout": 1,
                "output_policy": "summary",
            }
        ]
        correction = self.task("task-b", "long-running-command")
        correction["steps"] = [
            {
                "name": "attempt",
                "command": "long-running-command",
                "timeout": 120,
                "output_policy": "summary",
            }
        ]

        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp)
            task_dedupe.record_completion(state, first, "published", now_epoch=100)
            plan = task_dedupe.plan_pending(
                state,
                [(Path("b.json"), correction)],
                now_epoch=101,
            )

        self.assertEqual([item[1]["id"] for item in plan.candidates], ["task-b"])
        self.assertEqual(plan.suppressed, ())
        self.assertEqual(plan.invalid, ())

    def test_explicit_intent_key_reports_conflicting_plans_for_same_goal(self) -> None:
        first = self.task("task-a", "first-plan")
        second = self.task("task-b", "different-plan")
        first["dedupe_key"] = "example-project:nodeflow:diagnostics"
        second["dedupe_key"] = "example-project:nodeflow:diagnostics"
        pending = [(Path("a.json"), first), (Path("b.json"), second)]

        with tempfile.TemporaryDirectory() as tmp:
            plan = task_dedupe.plan_pending(Path(tmp), pending, now_epoch=100.0)

        self.assertEqual([item[1]["id"] for item in plan.candidates], ["task-a"])
        self.assertEqual(plan.suppressed, ())
        self.assertEqual(plan.invalid[0].reason, "dedupe_intent_conflict")
        self.assertIn("task-a", plan.invalid[0].error)

    def test_completed_failed_plan_requires_revision_for_correction(self) -> None:
        first = {**self.task("task-a", "false"), "dedupe_key": "fix"}
        correction = {**self.task("task-b", "true"), "dedupe_key": "fix"}
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp)
            task_dedupe.record_completion(state, first, "failed", now_epoch=100)
            conflict = task_dedupe.plan_pending(state, [(Path("b.json"), correction)], now_epoch=101)
            correction["dedupe_revision"] = 2
            revised = task_dedupe.plan_pending(state, [(Path("b.json"), correction)], now_epoch=102)
        self.assertEqual(conflict.candidates, ())
        self.assertEqual(conflict.invalid[0].reason, "dedupe_intent_conflict")
        self.assertEqual([item[1]["id"] for item in revised.candidates], ["task-b"])
        self.assertEqual(revised.invalid, ())

    def test_higher_revision_cannot_bypass_active_claim(self) -> None:
        first = {**self.task("task-a"), "dedupe_key": "fix"}
        correction = {**self.task("task-b", "false"), "dedupe_key": "fix", "dedupe_revision": 2}
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp)
            task_dedupe.record_admission(state, first, now_epoch=100)
            self.create_claim(state, "task-a")
            plan = task_dedupe.plan_pending(state, [(Path("b.json"), correction)], now_epoch=101)
        self.assertEqual(plan.candidates, ())
        self.assertEqual(plan.invalid[0].reason, "dedupe_intent_conflict")

    def test_explicit_revision_allows_deliberate_rerun_but_not_same_revision_duplicate(self) -> None:
        first = {**self.task("task-a"), "dedupe_key": "verify", "dedupe_revision": 2}
        duplicate = {**self.task("task-b"), "dedupe_key": "verify", "dedupe_revision": 2}
        rerun = {**self.task("task-c"), "dedupe_key": "verify", "dedupe_revision": 3}
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp)
            task_dedupe.record_completion(state, first, "success", now_epoch=100)
            repeated = task_dedupe.plan_pending(state, [(Path("b.json"), duplicate)], now_epoch=101)
            revised = task_dedupe.plan_pending(state, [(Path("c.json"), rerun)], now_epoch=101)
        self.assertEqual(repeated.candidates, ())
        self.assertEqual(repeated.suppressed[0].duplicate_of, "task-a")
        self.assertEqual([item[1]["id"] for item in revised.candidates], ["task-c"])

    def test_revision_requires_explicit_key_and_bounded_integer(self) -> None:
        for task in (
            {**self.task("task-a"), "dedupe_revision": 2},
            {**self.task("task-a"), "dedupe_key": "fix", "dedupe_revision": True},
            {**self.task("task-a"), "dedupe_key": "fix", "dedupe_revision": 0},
        ):
            with self.subTest(task=task), tempfile.TemporaryDirectory() as tmp:
                plan = task_dedupe.plan_pending(Path(tmp), [(Path("a.json"), task)])
                self.assertEqual(plan.candidates, ())
                self.assertEqual(plan.invalid[0].reason, "invalid_dedupe_key")

    def test_invalid_dedupe_key_is_rejected_without_blocking_other_tasks(self) -> None:
        invalid = self.task("task-a")
        invalid["dedupe_key"] = "bad key"
        valid = self.task("task-b", "echo valid")

        with tempfile.TemporaryDirectory() as tmp:
            plan = task_dedupe.plan_pending(
                Path(tmp),
                [(Path("a.json"), invalid), (Path("b.json"), valid)],
                now_epoch=100.0,
            )

        self.assertEqual([item[1]["id"] for item in plan.candidates], ["task-b"])
        self.assertEqual(plan.suppressed, ())
        self.assertEqual(len(plan.invalid), 1)
        self.assertEqual(plan.invalid[0].item[1]["id"], "task-a")
        self.assertIn("unsupported", plan.invalid[0].error)

    def test_recent_admission_suppresses_late_duplicate_from_another_chat(self) -> None:
        first = self.task("task-a")
        second = self.task("task-b")
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp)
            task_dedupe.record_admission(state_dir, first, now_epoch=100.0)
            self.create_claim(state_dir, "task-a")
            plan = task_dedupe.plan_pending(
                state_dir,
                [(Path("b.json"), second)],
                now_epoch=101.0,
            )

        self.assertEqual(plan.candidates, ())
        self.assertEqual(plan.suppressed[0].duplicate_of, "task-a")
        self.assertEqual(plan.suppressed[0].reason, "recent_duplicate")
        self.assertEqual(plan.invalid, ())

    def test_admission_receipt_expires_immediately_after_claim_disappears(self) -> None:
        first = self.task("task-a")
        second = self.task("task-b")
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp)
            task_dedupe.record_admission(state_dir, first, now_epoch=100.0)
            plan = task_dedupe.plan_pending(
                state_dir,
                [(Path("b.json"), second)],
                now_epoch=101.0,
            )

        self.assertEqual([item[1]["id"] for item in plan.candidates], ["task-b"])
        self.assertEqual(plan.suppressed, ())
        self.assertEqual(plan.invalid, ())

    def test_published_run_promotes_admission_after_claim_release_crash(self) -> None:
        first = self.task("task-a")
        second = self.task("task-b")
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp)
            task_dedupe.record_admission(state_dir, first, now_epoch=100.0)
            run_path = state_dir / "runs" / "task-a.json"
            run_path.parent.mkdir(parents=True, exist_ok=True)
            run_path.write_text(
                json.dumps(
                    {
                        "event": "result_published",
                        "task_digest": task_digest(first),
                        "status": "done",
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            plan = task_dedupe.plan_pending(
                state_dir,
                [(Path("b.json"), second)],
                now_epoch=101.0,
            )
            receipt = task_dedupe._read_receipt(
                state_dir,
                task_dedupe.queue_key(first),
                now_epoch=102.0,
            )

        self.assertEqual(plan.candidates, ())
        self.assertEqual(plan.suppressed[0].duplicate_of, "task-a")
        self.assertEqual(plan.suppressed[0].reason, "recent_duplicate")
        self.assertEqual(receipt["state"], "completed")
        self.assertEqual(receipt["outcome"], "published")
        self.assertTrue(receipt["reconciled_from_published_run"])
        self.assertEqual(
            receipt["reconciliation_reason"],
            "published_run_after_claim_release",
        )
        self.assertEqual(receipt["reconciled_at_epoch"], 101.0)

    def test_receipt_for_same_task_does_not_suppress_resource_retry(self) -> None:
        task = self.task("task-a")
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp)
            task_dedupe.record_admission(state_dir, task, now_epoch=100.0)
            plan = task_dedupe.plan_pending(
                state_dir,
                [(Path("a.json"), task)],
                now_epoch=101.0,
            )

        self.assertEqual([item[1]["id"] for item in plan.candidates], ["task-a"])
        self.assertEqual(plan.suppressed, ())
        self.assertEqual(plan.invalid, ())

    def test_completion_receipt_expires_after_bounded_window(self) -> None:
        first = self.task("task-a")
        second = self.task("task-b")
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp)
            task_dedupe.record_completion(state_dir, first, "published", now_epoch=100.0)
            recent = task_dedupe.plan_pending(
                state_dir,
                [(Path("b.json"), second)],
                now_epoch=100.0 + task_dedupe.RECENT_COMPLETION_TTL_SECONDS - 1,
            )
            expired = task_dedupe.plan_pending(
                state_dir,
                [(Path("b.json"), second)],
                now_epoch=100.0 + task_dedupe.RECENT_COMPLETION_TTL_SECONDS + 1,
            )

        self.assertEqual(recent.candidates, ())
        self.assertEqual([item[1]["id"] for item in expired.candidates], ["task-b"])
        self.assertEqual(expired.suppressed, ())
        self.assertEqual(expired.invalid, ())


if __name__ == "__main__":
    unittest.main()