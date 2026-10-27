from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import local_agent.operator.local as agent_operator
import local_agent.repository.worker as worker
import local_agent.daemon.service as agentd
from local_agent.repository.context import RepositoryContext

MATRIX_BINDING = "b5aeb06c-5752-4e7d-9189-150613ccd349"
C6_BINDING = "64877d7d-af3f-4312-a511-699c44aa42dd"


class SerialBindingAdmissionTests(unittest.TestCase):
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

    def test_serial_binding_ready_fails_closed_when_registry_is_unbound(self) -> None:
        unbound = RepositoryContext(
            self.repository.repository_id,
            self.repository.repository,
            self.repository.control,
            self.repository.work,
            self.repository.checkpoints,
            agent_binding=None,
        )
        with mock.patch.object(worker, "publish_repository_status") as publish:
            self.assertFalse(worker.repository_binding_ready(unbound))
        self.assertEqual(publish.call_args.args[1], "unbound")

    def test_serial_worker_rejects_wrong_binding_before_execution(self) -> None:
        task = {
            "id": "wrong-binding",
            "agent_binding": C6_BINDING,
            "mode": "commands",
            "allow_write": False,
            "resources": [],
            "command_timeout": 30,
            "task_timeout": 120,
            "commands": ["touch should-not-run"],
        }
        with mock.patch.object(worker, "bind_repository"), mock.patch.object(
            worker, "recover_claims_with_quarantine_layout"
        ), mock.patch.object(
            worker, "validate_repository_checkouts"
        ), mock.patch.object(worker, "sync_control_quietly"), mock.patch.object(
            agent_operator, "is_disabled", return_value=False
        ), mock.patch.object(
            worker, "repository_binding_ready", return_value=True
        ), mock.patch.object(agentd, "recover_stale_claims"), mock.patch.object(
            agentd, "recover_invalid_task_files"
        ), mock.patch.object(worker, "handle_repository_control"), mock.patch.object(
            agentd, "pending_tasks", return_value=[(Path("wrong-binding.json"), task)]
        ), mock.patch.object(worker.core, "publish_result") as publish_result, mock.patch.object(
            agentd, "publish_run_state"
        ), mock.patch.object(agentd, "execute_task") as execute:
            self.assertTrue(worker.poll_repository_once(self.repository))

        execute.assert_not_called()
        result = publish_result.call_args.args[1]
        self.assertEqual(result["failure_reason"], "agent_binding_mismatch")
        self.assertEqual(result["expected_agent_binding"], MATRIX_BINDING)
        self.assertEqual(result["provided_agent_binding"], C6_BINDING)


if __name__ == "__main__":
    unittest.main()
