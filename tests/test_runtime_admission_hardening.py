from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import local_agent.repository.binding as agent_binding
import local_agent.supervisor.worker as parallel_worker
from local_agent.repository.context import RepositoryContext

MATRIX_BINDING = "b5aeb06c-5752-4e7d-9189-150613ccd349"
OTHER_BINDING = "64877d7d-af3f-4312-a511-699c44aa42dd"


class RuntimeCatalogAdmissionTests(unittest.TestCase):
    def _write_catalog(self, root: Path, *, execution_enabled: bool, binding: str = MATRIX_BINDING) -> Path:
        path = root / "catalog.json"
        path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "agents": [
                        {
                            "id": "example-hub",
                            "repository": "example-owner/example-hub",
                            "agent_binding": binding,
                            "execution_enabled": execution_enabled,
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        return path

    def _write_control_binding(self, root: Path, *, binding: str = MATRIX_BINDING) -> Path:
        control = root / "control"
        (control / ".agent").mkdir(parents=True)
        (control / ".agent" / "binding.json").write_text(
            json.dumps(
                {
                    "version": 1,
                    "repository_id": "example-hub",
                    "repository": "example-owner/example-hub",
                    "agent_binding": binding,
                }
            ),
            encoding="utf-8",
        )
        return control

    def test_execution_disabled_catalog_blocks_stale_registry_and_control(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            catalog = self._write_catalog(root, execution_enabled=False)
            control = self._write_control_binding(root)
            with self.assertRaisesRegex(ValueError, "execution is disabled"):
                agent_binding.validate_repository_control_binding(
                    repository_id="example-hub",
                    repository="example-owner/example-hub",
                    expected_agent_binding=MATRIX_BINDING,
                    control_dir=control,
                    catalog_path=catalog,
                )

    def test_catalog_binding_remains_final_authority_over_stale_registry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            catalog = self._write_catalog(root, execution_enabled=True)
            control = self._write_control_binding(root, binding=OTHER_BINDING)
            with self.assertRaisesRegex(ValueError, "registry binding differs from canonical catalog"):
                agent_binding.validate_repository_control_binding(
                    repository_id="example-hub",
                    repository="example-owner/example-hub",
                    expected_agent_binding=OTHER_BINDING,
                    control_dir=control,
                    catalog_path=catalog,
                )


class BindingBeforeDedupeTests(unittest.TestCase):
    def test_misbound_task_is_removed_before_dedupe_can_poison_valid_intent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repository = RepositoryContext(
                "example-hub",
                "example-owner/example-hub",
                root / "control",
                root / "work",
                root / "checkpoints",
                agent_binding=MATRIX_BINDING,
            )
            misbound = {
                "id": "a-stale",
                "agent_binding": OTHER_BINDING,
                "dedupe_key": "same-intent",
                "work_branch": "main",
            }
            valid = {
                "id": "b-valid",
                "agent_binding": MATRIX_BINDING,
                "dedupe_key": "same-intent",
                "work_branch": "main",
            }
            pending = [
                (Path("a-stale.json"), misbound),
                (Path("b-valid.json"), valid),
            ]
            with mock.patch.object(parallel_worker, "_reject_task_binding") as reject:
                accepted = parallel_worker._filter_pending_task_bindings(repository, pending)

            self.assertEqual(accepted, [(Path("b-valid.json"), valid)])
            reject.assert_called_once_with(repository, misbound)

    def test_poll_passes_only_bound_tasks_into_dedupe_planner(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repository = RepositoryContext(
                "example-hub",
                "example-owner/example-hub",
                root / "control",
                root / "work",
                root / "checkpoints",
                agent_binding=MATRIX_BINDING,
            )
            misbound = {"id": "a-stale", "agent_binding": OTHER_BINDING}
            valid = {"id": "b-valid", "agent_binding": MATRIX_BINDING}
            pending = [(Path("a.json"), misbound), (Path("b.json"), valid)]

            with mock.patch.object(parallel_worker.serial_worker, "bind_repository"), mock.patch.object(
                parallel_worker.serial_worker, "validate_repository_checkouts"
            ), mock.patch.object(parallel_worker.serial_worker, "sync_control_quietly"), mock.patch.object(
                parallel_worker, "_repository_binding_ready", return_value=True
            ), mock.patch.object(parallel_worker.agentd, "recover_stale_claims"), mock.patch.object(
                parallel_worker.agentd, "recover_invalid_task_files"
            ), mock.patch.object(parallel_worker.serial_worker, "handle_repository_control"), mock.patch.object(
                parallel_worker.agent_operator, "is_disabled", return_value=False
            ), mock.patch.object(parallel_worker.agentd, "pending_tasks", return_value=pending), mock.patch.object(
                parallel_worker, "_reject_task_binding"
            ) as reject, mock.patch.object(
                parallel_worker, "_coalesce_pending_tasks", return_value=[]
            ) as coalesce, mock.patch.object(
                parallel_worker.agentd, "has_pending_publications", return_value=False
            ), mock.patch.object(parallel_worker.serial_worker, "publish_repository_status"):
                processed = parallel_worker.poll_repository_once(repository)

            self.assertFalse(processed)
            reject.assert_called_once_with(repository, misbound)
            coalesce.assert_called_once_with(repository, [(Path("b.json"), valid)])


if __name__ == "__main__":
    unittest.main()
