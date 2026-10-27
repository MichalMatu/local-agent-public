from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import local_agent.repository.binding as agent_binding
import local_agent.operator.local as agent_operator
import local_agent.supervisor.worker as agent_parallel_worker
import local_agent.foundation.storage as agent_storage
from local_agent.repository.context import RepositoryContext, repository_from_dict, validate_repository_set
from local_agent.runtime.task_contract import (
    require_task_agent_binding,
    task_agent_binding,
    validate_task,
)

MATRIX_BINDING = "b5aeb06c-5752-4e7d-9189-150613ccd349"
SHELLY_LINK_BINDING = "7f003af2-edde-4dd4-a32c-e9b038294d67"
C6_BINDING = "64877d7d-af3f-4312-a511-699c44aa42dd"


class AgentBindingHelpersTests(unittest.TestCase):
    def test_control_binding_rejects_noninteger_versions_and_unreadable_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / ".agent/binding.json"
            path.parent.mkdir()
            for version in (True, 1.0, "1", None):
                with self.subTest(version=version):
                    path.write_text(json.dumps({"version": version}))
                    with self.assertRaisesRegex(ValueError, "invalid repository control binding"):
                        agent_binding.control_binding_payload(root)
            with mock.patch.object(Path, "read_text", side_effect=PermissionError("denied")):
                with self.assertRaisesRegex(ValueError, "cannot be read"):
                    agent_binding.control_binding_payload(root)

    def test_canonical_binding_requires_lowercase_uuid(self) -> None:
        self.assertEqual(agent_binding.canonical_agent_binding(MATRIX_BINDING), MATRIX_BINDING)
        with self.assertRaisesRegex(ValueError, "canonical UUID"):
            agent_binding.canonical_agent_binding("example-hub")
        with self.assertRaisesRegex(ValueError, "canonical lowercase"):
            agent_binding.canonical_agent_binding(MATRIX_BINDING.upper())

    def test_catalog_is_unique_and_resolves_repository(self) -> None:
        records = agent_binding.load_binding_catalog()
        self.assertGreaterEqual(len(records), 6)
        self.assertEqual(len({record.agent_binding for record in records}), len(records))
        record = agent_binding.catalog_record_for_repository(
            "example-hub", "example-owner/example-hub"
        )
        self.assertEqual(record.agent_binding, MATRIX_BINDING)
        self.assertTrue(record.execution_enabled)
        shelly = agent_binding.catalog_record_for_repository(
            "example-device", "example-owner/example-device"
        )
        self.assertEqual(shelly.agent_binding, SHELLY_LINK_BINDING)
        self.assertTrue(shelly.execution_enabled)
        infra = agent_binding.catalog_record_for_repository(
            "local-agent", "example-owner/local-agent"
        )
        self.assertTrue(infra.execution_enabled)

    def test_registry_migration_adds_binding_but_refuses_existing_mismatch(self) -> None:
        payload = {
            "version": 1,
            "repositories": [
                {
                    "id": "example-hub",
                    "repository": "example-owner/example-hub",
                }
            ],
        }
        updated, changes = agent_binding.apply_catalog_to_registry_payload(payload)
        self.assertEqual(updated["repositories"][0]["agent_binding"], MATRIX_BINDING)
        self.assertEqual(changes[0]["repository_id"], "example-hub")

        payload["repositories"][0]["agent_binding"] = C6_BINDING
        with self.assertRaisesRegex(ValueError, "differs from canonical catalog"):
            agent_binding.apply_catalog_to_registry_payload(payload)

    def test_control_binding_must_match_all_three_identities(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            control = Path(tmp)
            (control / ".agent").mkdir()
            payload = {
                "version": 1,
                "repository_id": "example-hub",
                "repository": "example-owner/example-hub",
                "agent_binding": MATRIX_BINDING,
            }
            (control / ".agent" / "binding.json").write_text(
                json.dumps(payload), encoding="utf-8"
            )
            self.assertEqual(
                agent_binding.validate_repository_control_binding(
                    repository_id="example-hub",
                    repository="example-owner/example-hub",
                    expected_agent_binding=MATRIX_BINDING,
                    control_dir=control,
                ),
                MATRIX_BINDING,
            )
            payload["agent_binding"] = C6_BINDING
            (control / ".agent" / "binding.json").write_text(
                json.dumps(payload), encoding="utf-8"
            )
            with self.assertRaisesRegex(ValueError, "UUID mismatch"):
                agent_binding.validate_repository_control_binding(
                    repository_id="example-hub",
                    repository="example-owner/example-hub",
                    expected_agent_binding=MATRIX_BINDING,
                    control_dir=control,
                )


class RepositoryBindingTests(unittest.TestCase):
    def test_repository_registry_parses_binding_and_digest_identity(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            repository = repository_from_dict(
                {
                    "id": "example-hub",
                    "repository": "example-owner/example-hub",
                    "agent_binding": MATRIX_BINDING,
                },
                home=home,
            )
            self.assertEqual(repository.agent_binding, MATRIX_BINDING)
            self.assertEqual(repository.status_fields()["agent_binding"], MATRIX_BINDING)

    def test_duplicate_bindings_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = RepositoryContext(
                "a",
                "Owner/A",
                root / "a-control",
                root / "a-work",
                root / "a-checkpoints",
                agent_binding=MATRIX_BINDING,
            )
            second = RepositoryContext(
                "b",
                "Owner/B",
                root / "b-control",
                root / "b-work",
                root / "b-checkpoints",
                agent_binding=MATRIX_BINDING,
            )
            with self.assertRaisesRegex(ValueError, "duplicate agent binding"):
                validate_repository_set([first, second])


class TaskBindingContractTests(unittest.TestCase):
    @staticmethod
    def task(binding: str | None = MATRIX_BINDING) -> dict[str, object]:
        task: dict[str, object] = {
            "id": "binding-test",
            "mode": "commands",
            "allow_write": False,
            "resources": [],
            "command_timeout": 30,
            "task_timeout": 120,
            "commands": ["true"],
        }
        if binding is not None:
            task["agent_binding"] = binding
        return task

    def test_required_binding_missing_wrong_and_correct(self) -> None:
        missing = self.task(None)
        validate_task(missing)
        with self.assertRaisesRegex(ValueError, "agent_binding is required"):
            validate_task(missing, require_agent_binding=True)
        with self.assertRaisesRegex(ValueError, "agent_binding is required"):
            require_task_agent_binding(missing, MATRIX_BINDING)

        wrong = self.task(C6_BINDING)
        with self.assertRaisesRegex(ValueError, "agent_binding mismatch"):
            require_task_agent_binding(wrong, MATRIX_BINDING)

        correct = self.task(MATRIX_BINDING)
        validate_task(correct, require_agent_binding=True)
        self.assertEqual(task_agent_binding(correct), MATRIX_BINDING)
        self.assertEqual(require_task_agent_binding(correct, MATRIX_BINDING), MATRIX_BINDING)


class ParallelBindingAdmissionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.repository = RepositoryContext(
            "example-hub",
            "example-owner/example-hub",
            root / "control",
            root / "work",
            root / "checkpoints",
            agent_binding=MATRIX_BINDING,
        )
        (self.repository.control / ".agent").mkdir(parents=True)
        (self.repository.control / ".agent" / "binding.json").write_text(
            json.dumps(
                {
                    "version": 1,
                    "repository_id": "example-hub",
                    "repository": "example-owner/example-hub",
                    "agent_binding": MATRIX_BINDING,
                }
            ),
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_repository_binding_ready_fails_closed_when_registry_unbound(self) -> None:
        unbound = RepositoryContext(
            self.repository.repository_id,
            self.repository.repository,
            self.repository.control,
            self.repository.work,
            self.repository.checkpoints,
            agent_binding=None,
        )
        with mock.patch.object(
            agent_parallel_worker.serial_worker, "publish_repository_status"
        ) as publish:
            self.assertFalse(agent_parallel_worker._repository_binding_ready(unbound))
        self.assertEqual(publish.call_args.args[1], "unbound")

    def test_repository_binding_ready_rejects_control_binding_mismatch(self) -> None:
        payload = json.loads(
            (self.repository.control / ".agent" / "binding.json").read_text(encoding="utf-8")
        )
        payload["agent_binding"] = C6_BINDING
        (self.repository.control / ".agent" / "binding.json").write_text(
            json.dumps(payload), encoding="utf-8"
        )
        with mock.patch.object(
            agent_parallel_worker.serial_worker, "publish_repository_status"
        ) as publish:
            self.assertFalse(agent_parallel_worker._repository_binding_ready(self.repository))
        self.assertEqual(publish.call_args.args[1], "binding_error")

    def test_wrong_task_binding_publishes_terminal_rejection_without_execution(self) -> None:
        task = TaskBindingContractTests.task(C6_BINDING)
        with mock.patch.object(agent_parallel_worker.core, "publish_result") as publish_result, mock.patch.object(
            agent_parallel_worker.agentd, "publish_run_state"
        ) as publish_run:
            agent_parallel_worker._reject_task_binding(self.repository, task)
        result = publish_result.call_args.args[1]
        self.assertEqual(result["failure_reason"], "agent_binding_mismatch")
        self.assertEqual(result["expected_agent_binding"], MATRIX_BINDING)
        self.assertEqual(result["provided_agent_binding"], C6_BINDING)
        self.assertEqual(publish_run.call_args.args[0], "binding-test")


class OperatorBindingMigrationTests(unittest.TestCase):
    def test_migration_requires_disabled_and_writes_canonical_binding(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = root / "repositories.json"
            registry.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "repositories": [
                            {
                                "id": "example-hub",
                                "repository": "example-owner/example-hub",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            with mock.patch.object(agent_operator, "is_disabled", return_value=False):
                with self.assertRaisesRegex(RuntimeError, "requires Local Agent to be disabled"):
                    agent_operator.migrate_registry_bindings(registry_path=registry)
            with mock.patch.object(agent_operator, "is_disabled", return_value=True):
                result = agent_operator.migrate_registry_bindings(registry_path=registry)
            self.assertTrue(result["changed"])
            payload = json.loads(registry.read_text(encoding="utf-8"))
            self.assertEqual(payload["repositories"][0]["agent_binding"], MATRIX_BINDING)


class ControlRemoteTrackingRefTests(unittest.TestCase):
    def test_control_pull_updates_explicit_remote_tracking_ref(self) -> None:
        args = agent_storage.bounded_control_pull_args("agent-control")
        self.assertEqual(
            args[-1],
            "+refs/heads/agent-control:refs/remotes/origin/agent-control",
        )
        self.assertEqual(args[:6], ["pull", "--rebase", "--depth", "256", "--no-tags", "origin"])


if __name__ == "__main__":
    unittest.main()
