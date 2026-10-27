from __future__ import annotations

import contextlib
import json
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest import mock

import local_agent.repository.binding as binding
import local_agent.repository.worker as serial_worker
import local_agent.supervisor.worker as parallel_worker
from local_agent.repository.context import RepositoryContext

MATRIX_BINDING = "b5aeb06c-5752-4e7d-9189-150613ccd349"
OTHER_BINDING = "64877d7d-af3f-4312-a511-699c44aa42dd"


class RuntimeAdmissionProductionPathMatrixTests(unittest.TestCase):
    """Prove the shared execution boundary through both real worker poll paths."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def write_catalog(
        self,
        *,
        binding_value: str = MATRIX_BINDING,
        execution_enabled: bool = True,
    ) -> Path:
        path = self.root / "catalog.json"
        path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "agents": [
                        {
                            "id": "example-hub",
                            "repository": "example-owner/example-hub",
                            "agent_binding": binding_value,
                            "execution_enabled": execution_enabled,
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        return path

    def repository(self, *, binding_value: str = MATRIX_BINDING) -> RepositoryContext:
        repository = RepositoryContext(
            "example-hub",
            "example-owner/example-hub",
            self.root / "control",
            self.root / "work",
            self.root / "checkpoints",
            agent_binding=binding_value,
        )
        marker = repository.control / ".agent" / "binding.json"
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(
            json.dumps(
                {
                    "version": 1,
                    "repository_id": repository.repository_id,
                    "repository": repository.repository,
                    "agent_binding": binding_value,
                }
            ),
            encoding="utf-8",
        )
        return repository

    @staticmethod
    def task(task_binding: str = MATRIX_BINDING) -> dict[str, object]:
        return {
            "id": "matrix-task",
            "agent_binding": task_binding,
            "mode": "commands",
            "work_branch": "main",
            "allow_write": False,
            "resources": [],
            "commands": ["true"],
        }

    def run_poll(
        self,
        variant: str,
        repository: RepositoryContext,
        *,
        catalog: Path,
        pending: list[tuple[Path, dict[str, object]]],
    ) -> tuple[bool, mock.Mock, mock.Mock, mock.Mock]:
        execute = mock.Mock(return_value="done")
        publish_result = mock.Mock()
        publish_run = mock.Mock()

        with ExitStack() as stack:
            stack.enter_context(mock.patch.object(binding, "DEFAULT_CATALOG_PATH", catalog))
            # This matrix mocks repository binding/recovery to test only authorization.
            # Do not let the unrelated state-layout initializer consume shared globals.
            stack.enter_context(mock.patch.object(
                serial_worker, "recover_claims_with_quarantine_layout",
            ))

            if variant == "serial":
                stack.enter_context(mock.patch.object(serial_worker, "bind_repository"))
                stack.enter_context(mock.patch.object(serial_worker, "validate_repository_checkouts"))
                stack.enter_context(mock.patch.object(serial_worker, "sync_control_quietly"))
                stack.enter_context(mock.patch.object(serial_worker.agent_operator, "is_disabled", return_value=False))
                stack.enter_context(mock.patch.object(serial_worker.agentd, "recover_stale_claims"))
                stack.enter_context(mock.patch.object(serial_worker.agentd, "recover_invalid_task_files"))
                stack.enter_context(mock.patch.object(serial_worker, "handle_repository_control"))
                stack.enter_context(mock.patch.object(serial_worker.agentd, "pending_tasks", return_value=pending))
                stack.enter_context(mock.patch.object(serial_worker.agentd, "has_pending_publications", return_value=False))
                status = stack.enter_context(mock.patch.object(serial_worker, "publish_repository_status"))
                stack.enter_context(
                    mock.patch.object(
                        serial_worker,
                        "ActiveRepositoryControlWatcher",
                        side_effect=lambda *_args, **_kwargs: contextlib.nullcontext(),
                    )
                )
                stack.enter_context(mock.patch.object(serial_worker.agentd, "execute_task", execute))
                stack.enter_context(mock.patch.object(serial_worker.core, "publish_result", publish_result))
                stack.enter_context(mock.patch.object(serial_worker.agentd, "publish_run_state", publish_run))
                processed = serial_worker.poll_repository_once(repository)
            elif variant == "parallel":
                stack.enter_context(mock.patch.object(parallel_worker.serial_worker, "bind_repository"))
                stack.enter_context(mock.patch.object(parallel_worker.serial_worker, "validate_repository_checkouts"))
                stack.enter_context(mock.patch.object(parallel_worker.serial_worker, "sync_control_quietly"))
                stack.enter_context(mock.patch.object(parallel_worker.agent_operator, "is_disabled", return_value=False))
                stack.enter_context(mock.patch.object(parallel_worker.agentd, "recover_stale_claims"))
                stack.enter_context(mock.patch.object(parallel_worker.agentd, "recover_invalid_task_files"))
                stack.enter_context(mock.patch.object(parallel_worker.serial_worker, "handle_repository_control"))
                stack.enter_context(mock.patch.object(parallel_worker.agentd, "pending_tasks", return_value=pending))
                stack.enter_context(mock.patch.object(parallel_worker.agentd, "has_pending_publications", return_value=False))
                status = stack.enter_context(
                    mock.patch.object(parallel_worker.serial_worker, "publish_repository_status")
                )
                stack.enter_context(
                    mock.patch.object(
                        parallel_worker,
                        "_coalesce_pending_tasks",
                        side_effect=lambda _repository, items: list(items),
                    )
                )
                stack.enter_context(
                    mock.patch.object(
                        parallel_worker,
                        "machine_resource_lease",
                        side_effect=lambda _task: contextlib.nullcontext(()),
                    )
                )
                stack.enter_context(mock.patch.object(parallel_worker.task_dedupe, "record_admission"))
                stack.enter_context(mock.patch.object(parallel_worker.task_dedupe, "record_completion"))
                stack.enter_context(
                    mock.patch.object(
                        parallel_worker.serial_worker,
                        "ActiveRepositoryControlWatcher",
                        side_effect=lambda *_args, **_kwargs: contextlib.nullcontext(),
                    )
                )
                stack.enter_context(mock.patch.object(parallel_worker.agentd, "execute_task", execute))
                stack.enter_context(mock.patch.object(parallel_worker.core, "publish_result", publish_result))
                stack.enter_context(mock.patch.object(parallel_worker.agentd, "publish_run_state", publish_run))
                processed = parallel_worker.poll_repository_once(repository)
            else:
                raise AssertionError(f"unknown variant: {variant}")

        return processed, execute, publish_result, status

    def test_canonical_binding_executes_in_both_worker_paths(self) -> None:
        for variant in ("serial", "parallel"):
            with self.subTest(variant=variant):
                catalog = self.write_catalog()
                repository = self.repository()
                task = self.task()
                processed, execute, publish_result, status = self.run_poll(
                    variant,
                    repository,
                    catalog=catalog,
                    pending=[(Path("matrix-task.json"), task)],
                )

                self.assertTrue(processed)
                execute.assert_called_once()
                self.assertEqual(execute.call_args.args[0], task)
                publish_result.assert_not_called()
                self.assertTrue(any(call.args[1] == "idle" for call in status.call_args_list))

    def test_wrong_task_binding_is_terminally_rejected_without_execution(self) -> None:
        for variant in ("serial", "parallel"):
            with self.subTest(variant=variant):
                catalog = self.write_catalog()
                repository = self.repository()
                processed, execute, publish_result, _status = self.run_poll(
                    variant,
                    repository,
                    catalog=catalog,
                    pending=[(Path("wrong.json"), self.task(OTHER_BINDING))],
                )

                # The serial path reports a rejected selected task as processed,
                # while parallel filtering may empty the candidate list and return False.
                # Both are valid; the shared contract is terminal rejection before execution.
                self.assertIsInstance(processed, bool)
                execute.assert_not_called()
                result = publish_result.call_args.args[1]
                self.assertEqual(result["failure_reason"], "agent_binding_mismatch")
                self.assertEqual(result["expected_agent_binding"], MATRIX_BINDING)
                self.assertEqual(result["provided_agent_binding"], OTHER_BINDING)

    def test_execution_disabled_catalog_blocks_stale_registry_and_control(self) -> None:
        for variant in ("serial", "parallel"):
            with self.subTest(variant=variant):
                catalog = self.write_catalog(execution_enabled=False)
                repository = self.repository()
                processed, execute, publish_result, status = self.run_poll(
                    variant,
                    repository,
                    catalog=catalog,
                    pending=[(Path("matrix-task.json"), self.task())],
                )

                self.assertFalse(processed)
                execute.assert_not_called()
                publish_result.assert_not_called()
                self.assertTrue(any(call.args[1] == "binding_error" for call in status.call_args_list))

    def test_stale_registry_and_control_binding_cannot_override_catalog(self) -> None:
        for variant in ("serial", "parallel"):
            with self.subTest(variant=variant):
                catalog = self.write_catalog(binding_value=MATRIX_BINDING)
                repository = self.repository(binding_value=OTHER_BINDING)
                processed, execute, publish_result, status = self.run_poll(
                    variant,
                    repository,
                    catalog=catalog,
                    pending=[(Path("stale.json"), self.task(OTHER_BINDING))],
                )

                self.assertFalse(processed)
                execute.assert_not_called()
                publish_result.assert_not_called()
                self.assertTrue(any(call.args[1] == "binding_error" for call in status.call_args_list))


if __name__ == "__main__":
    unittest.main()
