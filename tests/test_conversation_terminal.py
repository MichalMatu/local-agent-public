from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from local_agent.conversation import contract, store, terminal
from local_agent.workflow import contract as workflow_contract
from local_agent.workflow.store import WorkflowStore


AGENT_BINDING = "12345678-1234-4abc-8def-1234567890ab"
PARENT_URL = "https://chatgpt.com/c/11111111-1111-4111-8111-111111111111"
CHILD_URL = "https://chatgpt.com/c/22222222-2222-4222-8222-222222222222"
REPOSITORY_COMMIT = "a" * 40
WORKFLOW_ID = "terminal-audit-44"
NODE_ID = "terminal-node-01"


def reasoning_workflow() -> dict:
    return {
        "schema_version": workflow_contract.WORKFLOW_SCHEMA_VERSION,
        "id": WORKFLOW_ID,
        "created_at": "2026-10-02T12:00:00Z",
        "nodes": [
            {
                "id": NODE_ID,
                "kind": "reasoning",
                "repository_id": "local-agent",
                "agent_binding": AGENT_BINDING,
                "depends_on": [],
            }
        ],
    }


def valid_request(workflow: dict) -> dict:
    return {
        "schema_version": contract.CHILD_REQUEST_SCHEMA_VERSION,
        "id": "child-terminal-001",
        "workflow_id": workflow["id"],
        "workflow_node_id": NODE_ID,
        "workflow_node_revision": 0,
        "workflow_node_introduction_digest": workflow_contract.manifest_digest(workflow),
        "parent_conversation_url": PARENT_URL,
        "created_at": "2026-10-02T12:01:00Z",
        "role": "verification",
        "repository_id": "local-agent",
        "agent_binding": AGENT_BINDING,
        "repository_ref": "develop/conversation-fabric",
        "repository_commit_sha": REPOSITORY_COMMIT,
        "scope": {
            "summary": "Record one bounded terminal evidence checkpoint.",
            "paths": ["local_agent/conversation/store.py"],
        },
        "context_refs": [],
        "bootstrap_contract_version": contract.BOOTSTRAP_CONTRACT_VERSION,
    }


def valid_registration(request: dict) -> dict:
    return {
        "schema_version": contract.CHILD_REGISTRATION_SCHEMA_VERSION,
        "child_request_id": request["id"],
        "child_request_digest": contract.child_request_digest(request),
        "parent_conversation_url": request["parent_conversation_url"],
        "child_conversation_url": CHILD_URL,
        "registered_at": "2026-10-02T12:02:00Z",
    }


def valid_terminal_record(
    request: dict,
    registration: dict,
    *,
    recorded_at: str = "2026-10-02T12:03:00Z",
) -> dict:
    return {
        "schema_version": terminal.TERMINAL_RECORD_SCHEMA_VERSION,
        "child_request_id": request["id"],
        "child_request_digest": contract.child_request_digest(request),
        "child_registration_digest": terminal.child_registration_digest(
            registration,
            request=request,
        ),
        "child_conversation_url": registration["child_conversation_url"],
        "summary": "The bounded child completed and durable evidence is available.",
        "evidence_refs": [
            {
                "kind": "result",
                "id": "terminal-result-001",
                "digest": "sha256:" + "7" * 64,
            }
        ],
        "recorded_at": recorded_at,
    }


class ConversationTerminalTests(unittest.TestCase):
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

    def registered_active_child(self) -> tuple[dict, dict]:
        request = valid_request(self.workflow)
        self.store.admit_request(request)
        self.store.transition_state(request["id"], "registration_pending")
        registration = valid_registration(request)
        self.store.register_child(registration)
        self.assertEqual(self.store.load_state(request["id"])["state"], "active")
        return request, registration

    def test_terminal_record_is_durable_and_advances_explicit_pending_state(self) -> None:
        request, registration = self.registered_active_child()
        pending = self.store.transition_state(
            request["id"],
            "terminal_pending_evidence",
        )
        self.assertEqual(pending["state"], "terminal_pending_evidence")

        record = valid_terminal_record(request, registration)
        self.assertEqual(self.store.record_terminal(record), record)
        self.assertEqual(
            self.store.load_state(request["id"])["state"],
            "terminal_recorded",
        )
        self.assertEqual(self.store.load_terminal_record(request["id"]), record)

        restarted = self.restarted_store()
        self.assertEqual(restarted.load_terminal_record(request["id"]), record)
        self.assertEqual(
            restarted.load_state(request["id"])["state"],
            "terminal_recorded",
        )

    def test_terminal_recorded_transition_requires_durable_evidence(self) -> None:
        request, _registration = self.registered_active_child()
        self.store.transition_state(request["id"], "terminal_pending_evidence")

        with self.assertRaisesRegex(ValueError, "requires durable terminal evidence"):
            self.store.transition_state(request["id"], "terminal_recorded")
        self.assertEqual(
            self.store.load_state(request["id"])["state"],
            "terminal_pending_evidence",
        )

    def test_terminal_record_requires_registration(self) -> None:
        request = valid_request(self.workflow)
        registration = valid_registration(request)
        self.store.admit_request(request)
        self.store.transition_state(request["id"], "registration_pending")

        with self.assertRaisesRegex(ValueError, "requires a durable registration"):
            self.store.record_terminal(valid_terminal_record(request, registration))
        self.assertIsNone(self.store.load_terminal_record(request["id"]))

    def test_terminal_record_rejects_invalid_lifecycle_ordering(self) -> None:
        request, registration = self.registered_active_child()
        record = valid_terminal_record(request, registration)

        with self.assertRaisesRegex(ValueError, "requires terminal_pending_evidence"):
            self.store.record_terminal(record)
        with self.assertRaisesRegex(ValueError, "invalid child lifecycle transition"):
            self.store.transition_state(request["id"], "terminal_recorded")
        self.assertEqual(self.store.load_state(request["id"])["state"], "active")

    def test_terminal_record_is_idempotent_and_conflicts_fail_closed(self) -> None:
        request, registration = self.registered_active_child()
        self.store.transition_state(request["id"], "terminal_pending_evidence")
        record = valid_terminal_record(request, registration)
        first = self.store.record_terminal(record)

        retry = valid_terminal_record(
            request,
            registration,
            recorded_at="2026-10-02T12:04:00Z",
        )
        self.assertEqual(self.store.record_terminal(retry), first)

        conflicting = copy.deepcopy(retry)
        conflicting["summary"] = "Conflicting terminal meaning."
        with self.assertRaisesRegex(ValueError, "conflicting terminal summary"):
            self.store.record_terminal(conflicting)
        self.assertEqual(self.store.load_terminal_record(request["id"]), first)
        self.assertEqual(
            self.store.load_state(request["id"])["state"],
            "terminal_recorded",
        )

    def test_terminal_record_binds_exact_request_and_canonical_registration(self) -> None:
        request, registration = self.registered_active_child()
        self.store.transition_state(request["id"], "terminal_pending_evidence")

        wrong_request = valid_terminal_record(request, registration)
        wrong_request["child_request_digest"] = "sha256:" + "0" * 64
        with self.assertRaisesRegex(ValueError, "does not match admitted request"):
            self.store.record_terminal(wrong_request)

        wrong_registration = valid_terminal_record(request, registration)
        wrong_registration["child_registration_digest"] = "sha256:" + "1" * 64
        with self.assertRaisesRegex(ValueError, "does not match registration"):
            self.store.record_terminal(wrong_registration)

        wrong_url = valid_terminal_record(request, registration)
        wrong_url["child_conversation_url"] = (
            "https://chatgpt.com/c/33333333-3333-4333-8333-333333333333"
        )
        with self.assertRaisesRegex(ValueError, "does not match registration"):
            self.store.record_terminal(wrong_url)

        self.assertIsNone(self.store.load_terminal_record(request["id"]))
        self.assertEqual(
            self.store.load_state(request["id"])["state"],
            "terminal_pending_evidence",
        )

    def test_terminal_evidence_is_nonempty_bounded_and_schema_validated(self) -> None:
        request, registration = self.registered_active_child()
        self.store.transition_state(request["id"], "terminal_pending_evidence")

        missing = valid_terminal_record(request, registration)
        missing["evidence_refs"] = []
        with self.assertRaisesRegex(ValueError, "non-empty list"):
            self.store.record_terminal(missing)

        oversized = valid_terminal_record(request, registration)
        oversized["summary"] = "x" * (terminal.MAX_TERMINAL_SUMMARY_CHARS + 1)
        with self.assertRaisesRegex(ValueError, "bounded string"):
            self.store.record_terminal(oversized)

        malformed = valid_terminal_record(request, registration)
        malformed["evidence_refs"][0]["extra"] = True
        with self.assertRaisesRegex(ValueError, "fields do not match schema"):
            self.store.record_terminal(malformed)

        self.assertIsNone(self.store.load_terminal_record(request["id"]))

    def test_terminal_record_write_before_state_failure_recovers_idempotently(self) -> None:
        request, registration = self.registered_active_child()
        self.store.transition_state(request["id"], "terminal_pending_evidence")
        record = valid_terminal_record(request, registration)
        actual_atomic_write = store.atomic_write_text
        state_path = self.store._state_path(request["id"])

        def fail_terminal_state(path: Path, text: str) -> None:
            payload = json.loads(text)
            if path == state_path and payload.get("state") == "terminal_recorded":
                raise OSError("simulated crash after terminal evidence write")
            actual_atomic_write(path, text)

        with mock.patch.object(store, "atomic_write_text", side_effect=fail_terminal_state):
            with self.assertRaisesRegex(OSError, "simulated crash"):
                self.store.record_terminal(record)

        restarted = self.restarted_store()
        self.assertEqual(restarted.load_terminal_record(request["id"]), record)
        self.assertEqual(
            restarted.load_state(request["id"])["state"],
            "terminal_pending_evidence",
        )
        retry = valid_terminal_record(
            request,
            registration,
            recorded_at="2026-10-02T12:05:00Z",
        )
        self.assertEqual(restarted.record_terminal(retry), record)
        self.assertEqual(
            restarted.load_state(request["id"])["state"],
            "terminal_recorded",
        )

    def test_terminal_recorded_reload_fails_closed_without_evidence(self) -> None:
        request, registration = self.registered_active_child()
        self.store.transition_state(request["id"], "terminal_pending_evidence")
        self.store.record_terminal(valid_terminal_record(request, registration))
        self.store._terminal_path(request["id"]).unlink()

        restarted = self.restarted_store()
        with self.assertRaisesRegex(ValueError, "requires durable terminal evidence"):
            restarted.load_state(request["id"])

    def test_terminal_record_mutation_reuses_workflow_execution_lock(self) -> None:
        request, registration = self.registered_active_child()
        self.store.transition_state(request["id"], "terminal_pending_evidence")
        record = valid_terminal_record(request, registration)

        with mock.patch.object(
            self.workflow_store,
            "execution_lock",
            wraps=self.workflow_store.execution_lock,
        ) as execution_lock:
            self.store.record_terminal(record)

        execution_lock.assert_called_once_with(self.workflow["id"])


if __name__ == "__main__":
    unittest.main()
