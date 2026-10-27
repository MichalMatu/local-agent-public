from __future__ import annotations

import contextlib
import json
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest import mock

import local_agent.supervisor.worker as worker
from local_agent.repository.context import RepositoryContext

BINDING = "b5aeb06c-5752-4e7d-9189-150613ccd349"


class ParallelProductionIngestionDedupeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.repository = RepositoryContext(
            repository_id="example-hub",
            repository="example-owner/example-hub",
            control=self.root / "control",
            work=self.root / "work",
            checkpoints=self.root / "checkpoints",
            agent_binding=BINDING,
        )
        (self.repository.control / ".agent" / "tasks").mkdir(parents=True)
        (self.repository.control / ".agent" / "results").mkdir(parents=True)
        self.state_dir = self.root / "state"

    @staticmethod
    def task(
        task_id: str,
        *,
        command: str = "true",
        dedupe_key: str = "shared-intent",
    ) -> dict[str, object]:
        return {
            "id": task_id,
            "agent_binding": BINDING,
            "mode": "commands",
            "work_branch": "main",
            "allow_write": False,
            "resources": [],
            "command_timeout": 30,
            "task_timeout": 120,
            "commands": [command],
            "dedupe_key": dedupe_key,
        }

    def common_poll_stack(self) -> tuple[ExitStack, mock.Mock, mock.Mock, mock.Mock]:
        stack = ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(mock.patch.object(worker.serial_worker, "bind_repository"))
        stack.enter_context(mock.patch.object(worker.serial_worker, "validate_repository_checkouts"))
        stack.enter_context(mock.patch.object(worker.serial_worker, "sync_control_quietly"))
        stack.enter_context(mock.patch.object(worker.agent_operator, "is_disabled", return_value=False))
        stack.enter_context(mock.patch.object(worker, "_repository_binding_ready", return_value=True))
        stack.enter_context(mock.patch.object(worker.agentd, "recover_stale_claims"))
        stack.enter_context(mock.patch.object(worker.serial_worker, "handle_repository_control"))
        stack.enter_context(
            mock.patch.object(
                worker.serial_worker,
                "publish_repository_status",
            )
        )
        stack.enter_context(
            mock.patch.object(
                worker.serial_worker,
                "repository_state_dir",
                return_value=self.state_dir,
            )
        )
        stack.enter_context(
            mock.patch.object(
                worker,
                "machine_resource_lease",
                side_effect=lambda _task: contextlib.nullcontext(()),
            )
        )
        stack.enter_context(
            mock.patch.object(
                worker.serial_worker,
                "ActiveRepositoryControlWatcher",
                side_effect=lambda *_args, **_kwargs: contextlib.nullcontext(),
            )
        )
        execute = stack.enter_context(mock.patch.object(worker.agentd, "execute_task", return_value="done"))
        publish_result = stack.enter_context(mock.patch.object(worker.core, "publish_result"))
        publish_run = stack.enter_context(mock.patch.object(worker.agentd, "publish_run_state"))
        return stack, execute, publish_result, publish_run

    def test_malformed_dedupe_manifest_is_rejected_by_real_poll_ingestion(self) -> None:
        task = self.task("bad-dedupe", dedupe_key="bad key")
        manifest = self.repository.control / ".agent" / "tasks" / "bad-dedupe.json"
        manifest.write_text(json.dumps(task), encoding="utf-8")

        stack, execute, publish_result, publish_run = self.common_poll_stack()
        with stack, mock.patch.object(
            worker.agentd.core,
            "CONTROL",
            self.repository.control,
        ), mock.patch.object(
            worker.agentd,
            "has_pending_publications",
            return_value=False,
        ):
            processed = worker.poll_repository_once(self.repository)

        self.assertFalse(processed)
        execute.assert_not_called()
        publish_result.assert_called_once()
        result = publish_result.call_args.args[1]
        self.assertEqual(result["id"], "bad-dedupe")
        self.assertEqual(result["failure_reason"], "invalid_dedupe_key")
        self.assertIn("InvalidDedupeMetadata", result["error"])
        publish_run.assert_called_once()
        self.assertEqual(
            publish_run.call_args.args[1]["failure_reason"],
            "invalid_dedupe_key",
        )

    def test_conflicting_same_revision_intent_executes_only_first_candidate(self) -> None:
        first = self.task("task-a", command="true")
        conflicting = self.task("task-b", command="false")
        pending = [
            (Path("task-a.json"), first),
            (Path("task-b.json"), conflicting),
        ]

        stack, execute, publish_result, publish_run = self.common_poll_stack()
        with stack, mock.patch.object(
            worker.agentd,
            "recover_invalid_task_files",
        ), mock.patch.object(
            worker.agentd,
            "pending_tasks",
            return_value=pending,
        ), mock.patch.object(
            worker.task_dedupe,
            "record_admission",
        ), mock.patch.object(
            worker.task_dedupe,
            "record_completion",
        ):
            processed = worker.poll_repository_once(self.repository)

        self.assertTrue(processed)
        execute.assert_called_once()
        self.assertEqual(execute.call_args.args[0], first)
        publish_result.assert_called_once()
        rejected = publish_result.call_args.args[1]
        self.assertEqual(rejected["id"], "task-b")
        self.assertEqual(rejected["status"], "failed")
        self.assertEqual(rejected["failure_reason"], "dedupe_intent_conflict")
        self.assertEqual(publish_run.call_count, 1)
        self.assertEqual(
            publish_run.call_args.args[1]["failure_reason"],
            "dedupe_intent_conflict",
        )


if __name__ == "__main__":
    unittest.main()
