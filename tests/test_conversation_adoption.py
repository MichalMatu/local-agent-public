from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from local_agent.conversation import adoption, contract, store
from local_agent.workflow.store import WorkflowStore
from tests.test_conversation_terminal import (
    NODE_ID,
    reasoning_workflow,
    valid_registration,
    valid_request,
    valid_terminal_record,
)


def valid_adoption_record(
    request: dict,
    registration: dict,
    terminal_record: dict,
    *,
    adopted_at: str = "2026-10-02T12:04:00Z",
) -> dict:
    return {
        "schema_version": adoption.ADOPTION_RECORD_SCHEMA_VERSION,
        "child_request_id": request["id"],
        "child_request_digest": contract.child_request_digest(request),
        "terminal_record_digest": adoption.terminal_record_digest(
            terminal_record,
            request=request,
            registration=registration,
        ),
        "workflow_id": request["workflow_id"],
        "workflow_node_id": request["workflow_node_id"],
        "adopted_at": adopted_at,
    }


class ConversationAdoptionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state_dir = Path(self.tmp.name)
        self.workflow_store = WorkflowStore(self.state_dir)
        self.workflow = reasoning_workflow()
        self.workflow_store.submit(self.workflow)
        self.store = store.WorkflowConversationStore(
            self.workflow_store,
            self.workflow["id"],
        )

    def restarted_store(self) -> store.WorkflowConversationStore:
        return store.WorkflowConversationStore(
            WorkflowStore(self.state_dir),
            self.workflow["id"],
        )

    def terminal_recorded_child(self) -> tuple[dict, dict, dict]:
        request = valid_request(self.workflow)
        self.store.admit_request(request)
        self.store.transition_state(request["id"], "registration_pending")
        registration = valid_registration(request)
        self.store.register_child(registration)
        self.store.transition_state(request["id"], "terminal_pending_evidence")
        terminal_record = valid_terminal_record(request, registration)
        self.store.record_terminal(terminal_record)
        self.assertEqual(
            self.store.load_state(request["id"])["state"],
            "terminal_recorded",
        )
        return request, registration, terminal_record

    def test_adoption_is_durable_advances_workflow_and_allows_retirement(self) -> None:
        request, registration, terminal_record = self.terminal_recorded_child()
        record = valid_adoption_record(request, registration, terminal_record)

        self.assertEqual(self.store.record_adoption(record), record)
        self.assertEqual(
            self.workflow_store.load_state(self.workflow["id"])["node_states"][NODE_ID],
            "succeeded",
        )
        self.assertEqual(
            self.store.load_state(request["id"])["state"],
            "terminal_recorded",
        )

        retired = self.store.transition_state(request["id"], "retired")
        self.assertEqual(retired["state"], "retired")

        restarted = self.restarted_store()
        self.assertEqual(restarted.load_adoption_record(request["id"]), record)
        self.assertEqual(restarted.load_state(request["id"])["state"], "retired")
        self.assertEqual(
            restarted.workflow_store.load_state(self.workflow["id"])["node_states"][NODE_ID],
            "succeeded",
        )

    def test_adoption_requires_durable_terminal_record(self) -> None:
        request = valid_request(self.workflow)
        registration = valid_registration(request)
        terminal_record = valid_terminal_record(request, registration)
        self.store.admit_request(request)
        self.store.transition_state(request["id"], "registration_pending")
        self.store.register_child(registration)

        with self.assertRaisesRegex(ValueError, "requires durable terminal evidence"):
            self.store.record_adoption(
                valid_adoption_record(request, registration, terminal_record)
            )
        self.assertIsNone(self.store.load_adoption_record(request["id"]))
        self.assertEqual(
            self.workflow_store.load_state(self.workflow["id"])["node_states"][NODE_ID],
            "waiting_conversation",
        )

    def test_retirement_requires_durable_adoption(self) -> None:
        request, _registration, _terminal_record = self.terminal_recorded_child()

        with self.assertRaisesRegex(ValueError, "requires durable adoption"):
            self.store.transition_state(request["id"], "retired")
        self.assertEqual(
            self.store.load_state(request["id"])["state"],
            "terminal_recorded",
        )

    def test_adoption_binds_exact_request_terminal_and_reasoning_node(self) -> None:
        request, registration, terminal_record = self.terminal_recorded_child()

        wrong_request = valid_adoption_record(request, registration, terminal_record)
        wrong_request["child_request_digest"] = "sha256:" + "0" * 64
        with self.assertRaisesRegex(ValueError, "does not match admitted request"):
            self.store.record_adoption(wrong_request)

        wrong_terminal = valid_adoption_record(request, registration, terminal_record)
        wrong_terminal["terminal_record_digest"] = "sha256:" + "1" * 64
        with self.assertRaisesRegex(ValueError, "does not match durable terminal record"):
            self.store.record_adoption(wrong_terminal)

        wrong_node = valid_adoption_record(request, registration, terminal_record)
        wrong_node["workflow_node_id"] = "another-node"
        with self.assertRaisesRegex(ValueError, "does not match admitted request"):
            self.store.record_adoption(wrong_node)

        self.assertIsNone(self.store.load_adoption_record(request["id"]))
        self.assertEqual(
            self.workflow_store.load_state(self.workflow["id"])["node_states"][NODE_ID],
            "waiting_conversation",
        )

    def test_adoption_is_idempotent_and_conflicts_fail_closed(self) -> None:
        request, registration, terminal_record = self.terminal_recorded_child()
        first = valid_adoption_record(request, registration, terminal_record)
        self.assertEqual(self.store.record_adoption(first), first)

        retry = valid_adoption_record(
            request,
            registration,
            terminal_record,
            adopted_at="2026-10-02T12:05:00Z",
        )
        self.assertEqual(self.store.record_adoption(retry), first)

        conflicting = copy.deepcopy(retry)
        conflicting["terminal_record_digest"] = "sha256:" + "3" * 64
        with self.assertRaisesRegex(ValueError, "does not match durable terminal record"):
            self.store.record_adoption(conflicting)
        self.assertEqual(self.store.load_adoption_record(request["id"]), first)

    def test_adoption_write_before_workflow_state_failure_recovers_idempotently(self) -> None:
        request, registration, terminal_record = self.terminal_recorded_child()
        record = valid_adoption_record(request, registration, terminal_record)

        with mock.patch.object(
            self.workflow_store,
            "set_node_state",
            side_effect=OSError("simulated crash after adoption write"),
        ):
            with self.assertRaisesRegex(OSError, "simulated crash"):
                self.store.record_adoption(record)

        restarted = self.restarted_store()
        self.assertEqual(restarted.load_adoption_record(request["id"]), record)
        self.assertEqual(
            restarted.workflow_store.load_state(self.workflow["id"])["node_states"][NODE_ID],
            "waiting_conversation",
        )
        self.assertEqual(
            restarted.load_state(request["id"])["state"],
            "terminal_recorded",
        )

        retry = valid_adoption_record(
            request,
            registration,
            terminal_record,
            adopted_at="2026-10-02T12:06:00Z",
        )
        self.assertEqual(restarted.record_adoption(retry), record)
        self.assertEqual(
            restarted.workflow_store.load_state(self.workflow["id"])["node_states"][NODE_ID],
            "succeeded",
        )
        self.assertEqual(
            restarted.transition_state(request["id"], "retired")["state"],
            "retired",
        )

    def test_retired_reload_fails_closed_without_adoption_record(self) -> None:
        request, registration, terminal_record = self.terminal_recorded_child()
        self.store.record_adoption(
            valid_adoption_record(request, registration, terminal_record)
        )
        self.store.transition_state(request["id"], "retired")
        self.store._adoption_path(request["id"]).unlink()

        restarted = self.restarted_store()
        with self.assertRaisesRegex(ValueError, "requires durable adoption"):
            restarted.load_state(request["id"])

    def test_retired_reload_fails_closed_if_workflow_node_is_not_succeeded(self) -> None:
        request, registration, terminal_record = self.terminal_recorded_child()
        self.store.record_adoption(
            valid_adoption_record(request, registration, terminal_record)
        )
        self.store.transition_state(request["id"], "retired")

        workflow_path = self.workflow_store._state_path(self.workflow["id"])
        payload = json.loads(workflow_path.read_text(encoding="utf-8"))
        payload["node_states"][NODE_ID] = "waiting_conversation"
        payload["workflow_state"] = "waiting_conversation"
        workflow_path.write_text(
            json.dumps(payload, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

        restarted = self.restarted_store()
        with self.assertRaisesRegex(
            ValueError,
            "requires adopted workflow node to be succeeded",
        ):
            restarted.load_state(request["id"])

    def test_adoption_mutation_reuses_workflow_execution_lock(self) -> None:
        request, registration, terminal_record = self.terminal_recorded_child()
        record = valid_adoption_record(request, registration, terminal_record)

        with mock.patch.object(
            self.workflow_store,
            "execution_lock",
            wraps=self.workflow_store.execution_lock,
        ) as execution_lock:
            self.store.record_adoption(record)

        execution_lock.assert_called_once_with(self.workflow["id"])


if __name__ == "__main__":
    unittest.main()
