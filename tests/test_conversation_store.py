from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from local_agent.conversation import contract
from local_agent.conversation import store
from local_agent.workflow import contract as workflow_contract
from local_agent.workflow.store import WorkflowStore


AGENT_BINDING = "12345678-1234-4abc-8def-1234567890ab"
PARENT_URL = "https://chatgpt.com/c/11111111-1111-4111-8111-111111111111"
CHILD_URL = "https://chatgpt.com/c/22222222-2222-4222-8222-222222222222"
CONTEXT_DIGEST = "sha256:" + "2" * 64
REPOSITORY_COMMIT = "a" * 40
WORKFLOW_ID = "audit-44"
NODE_ID = "audit-node-01"


def reasoning_workflow(workflow_id: str = WORKFLOW_ID) -> dict:
    return {
        "schema_version": workflow_contract.WORKFLOW_SCHEMA_VERSION,
        "id": workflow_id,
        "created_at": "2026-09-23T19:59:00Z",
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


def valid_request(
    workflow: dict,
    request_id: str = "child-audit-001",
) -> dict:
    return {
        "schema_version": contract.CHILD_REQUEST_SCHEMA_VERSION,
        "id": request_id,
        "workflow_id": workflow["id"],
        "workflow_node_id": NODE_ID,
        "workflow_node_revision": 0,
        "workflow_node_introduction_digest": workflow_contract.manifest_digest(workflow),
        "parent_conversation_url": PARENT_URL,
        "created_at": "2026-09-23T20:00:00Z",
        "role": "research",
        "repository_id": "local-agent",
        "agent_binding": AGENT_BINDING,
        "repository_ref": "develop/conversation-fabric",
        "repository_commit_sha": REPOSITORY_COMMIT,
        "scope": {
            "summary": "Audit one bounded module without importing unrelated transcript history.",
            "paths": ["local_agent/workflow/contract.py"],
        },
        "context_refs": [
            {
                "kind": "finding",
                "id": "finding-001",
                "digest": CONTEXT_DIGEST,
            }
        ],
        "bootstrap_contract_version": contract.BOOTSTRAP_CONTRACT_VERSION,
    }


def valid_registration(
    request: dict,
    *,
    registered_at: str = "2026-09-23T20:01:00Z",
) -> dict:
    return {
        "schema_version": contract.CHILD_REGISTRATION_SCHEMA_VERSION,
        "child_request_id": request["id"],
        "child_request_digest": contract.child_request_digest(request),
        "parent_conversation_url": request["parent_conversation_url"],
        "child_conversation_url": CHILD_URL,
        "registered_at": registered_at,
    }


class ConversationStoreTests(unittest.TestCase):
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

    def test_store_is_owned_by_one_workflow(self) -> None:
        self.assertEqual(
            self.store.root,
            self.workflow_store.root / self.workflow["id"] / "conversations",
        )
        self.assertFalse((self.state_dir / "conversations").exists())

    def test_admit_request_persists_request_and_initial_state_idempotently(self) -> None:
        request = valid_request(self.workflow)
        first = self.store.admit_request(request)
        retried = copy.deepcopy(request)
        retried["created_at"] = "2026-09-23T21:00:00Z"
        second = self.store.admit_request(retried)

        self.assertEqual(first, second)
        self.assertEqual(first["state"], "requested")
        self.assertEqual(
            first["child_request_digest"],
            contract.child_request_digest(request),
        )
        self.assertEqual(self.store.load_request(request["id"]), request)
        self.assertEqual(self.store.request_ids(), [request["id"]])
        self.assertEqual(self.store.request_for_node(NODE_ID), request)

        restarted = self.restarted_store()
        self.assertEqual(restarted.load_request(request["id"]), request)
        self.assertEqual(restarted.load_state(request["id"]), first)
        self.assertEqual(restarted.request_for_node(NODE_ID), request)

    def test_admission_requires_exact_reasoning_node_provenance_and_target(self) -> None:
        request = valid_request(self.workflow)
        request["workflow_node_introduction_digest"] = "sha256:" + "0" * 64
        with self.assertRaisesRegex(ValueError, "introduction_digest mismatch"):
            self.store.admit_request(request)

        request = valid_request(self.workflow)
        request["repository_id"] = "other-repository"
        with self.assertRaisesRegex(ValueError, "repository mismatch"):
            self.store.admit_request(request)

        request = valid_request(self.workflow)
        request["agent_binding"] = "00000000-0000-4000-8000-000000000099"
        with self.assertRaisesRegex(ValueError, "agent_binding mismatch"):
            self.store.admit_request(request)

    def test_request_for_non_reasoning_workflow_node_is_rejected(self) -> None:
        workflow = {
            "schema_version": workflow_contract.WORKFLOW_SCHEMA_VERSION,
            "id": "task-workflow",
            "created_at": "2026-09-23T19:59:00Z",
            "nodes": [
                {
                    "id": NODE_ID,
                    "kind": "task",
                    "repository_id": "local-agent",
                    "agent_binding": AGENT_BINDING,
                    "depends_on": [],
                    "task": {
                        "work_branch": "main",
                        "allow_write": False,
                        "resources": [],
                        "commands": ["true"],
                    },
                }
            ],
        }
        self.workflow_store.submit(workflow)
        task_store = store.WorkflowConversationStore(self.workflow_store, workflow["id"])
        with self.assertRaisesRegex(ValueError, "must be a reasoning node"):
            task_store.admit_request(valid_request(workflow))

    def test_same_request_id_can_exist_in_different_workflows_without_collision(self) -> None:
        first = valid_request(self.workflow)
        self.store.admit_request(first)

        second_workflow = reasoning_workflow("audit-45")
        self.workflow_store.submit(second_workflow)
        second_store = store.WorkflowConversationStore(
            self.workflow_store,
            second_workflow["id"],
        )
        second = valid_request(second_workflow)
        second_store.admit_request(second)

        self.assertEqual(self.store.load_request(first["id"]), first)
        self.assertEqual(second_store.load_request(second["id"]), second)
        self.assertNotEqual(self.store.root, second_store.root)

    def test_same_request_id_with_different_semantics_is_rejected(self) -> None:
        request = valid_request(self.workflow)
        self.store.admit_request(request)
        changed = copy.deepcopy(request)
        changed["repository_commit_sha"] = "b" * 40
        with self.assertRaisesRegex(ValueError, "different digest"):
            self.store.admit_request(changed)

    def test_reasoning_node_accepts_only_one_durable_child_request(self) -> None:
        first = valid_request(self.workflow, "child-a")
        second = valid_request(self.workflow, "child-b")
        self.store.admit_request(first)

        with self.assertRaisesRegex(ValueError, "already has child request 'child-a'"):
            self.store.admit_request(second)

        self.assertEqual(self.store.request_ids(), ["child-a"])
        self.assertEqual(self.store.request_for_node(NODE_ID), first)

    def test_request_identity_cannot_escape_store_root(self) -> None:
        with self.assertRaisesRegex(ValueError, "child request id"):
            self.store.load_request("../outside")

    def test_corruption_is_isolated_to_one_workflow_request(self) -> None:
        first = valid_request(self.workflow, "child-a")
        self.store.admit_request(first)

        second_workflow = reasoning_workflow("audit-corruption-2")
        self.workflow_store.submit(second_workflow)
        second_store = store.WorkflowConversationStore(
            self.workflow_store,
            second_workflow["id"],
        )
        second = valid_request(second_workflow, "child-b")
        second_store.admit_request(second)
        self.store._request_path(first["id"]).write_text("{bad", encoding="utf-8")

        with self.assertRaisesRegex(ValueError, "invalid child request"):
            self.store.load_request(first["id"])
        self.assertEqual(second_store.load_request(second["id"]), second)

    def test_transition_state_is_durable_and_active_requires_registration(self) -> None:
        request = valid_request(self.workflow)
        self.store.admit_request(request)
        pending = self.store.transition_state(request["id"], "registration_pending")
        self.assertEqual(pending["state"], "registration_pending")

        with self.assertRaisesRegex(ValueError, "requires a durable registration"):
            self.store.transition_state(request["id"], "active")

        restarted = self.restarted_store()
        self.assertEqual(
            restarted.load_state(request["id"])["state"],
            "registration_pending",
        )

    def test_registration_is_create_once_idempotent_and_advances_state(self) -> None:
        request = valid_request(self.workflow)
        self.store.admit_request(request)
        self.store.transition_state(request["id"], "registration_pending")
        registration = valid_registration(request)
        first = self.store.register_child(registration)
        self.assertEqual(first, registration)
        self.assertEqual(self.store.load_state(request["id"])["state"], "active")

        retry = valid_registration(request, registered_at="2026-09-23T20:02:00Z")
        second = self.store.register_child(retry)
        self.assertEqual(second, registration)
        self.assertEqual(self.store.load_registration(request["id"]), registration)

        conflicting = copy.deepcopy(registration)
        conflicting["child_conversation_url"] = (
            "https://chatgpt.com/c/33333333-3333-4333-8333-333333333333"
        )
        with self.assertRaisesRegex(ValueError, "conflicting child conversation URL"):
            self.store.register_child(conflicting)

    def test_registration_requires_pending_state(self) -> None:
        request = valid_request(self.workflow)
        self.store.admit_request(request)
        with self.assertRaisesRegex(ValueError, "requires registration_pending"):
            self.store.register_child(valid_registration(request))

    def test_registration_write_before_state_failure_recovers_on_retry(self) -> None:
        request = valid_request(self.workflow)
        self.store.admit_request(request)
        self.store.transition_state(request["id"], "registration_pending")
        registration = valid_registration(request)
        actual_atomic_write = store.atomic_write_text
        active_state_path = self.store._state_path(request["id"])

        def fail_active_state(path: Path, text: str) -> None:
            payload = json.loads(text)
            if path == active_state_path and payload.get("state") == "active":
                raise OSError("simulated crash after registration write")
            actual_atomic_write(path, text)

        with mock.patch.object(store, "atomic_write_text", side_effect=fail_active_state):
            with self.assertRaisesRegex(OSError, "simulated crash"):
                self.store.register_child(registration)

        restarted = self.restarted_store()
        self.assertEqual(restarted.load_registration(request["id"]), registration)
        self.assertEqual(
            restarted.load_state(request["id"])["state"],
            "registration_pending",
        )
        self.assertEqual(restarted.register_child(registration), registration)
        self.assertEqual(restarted.load_state(request["id"])["state"], "active")

    def test_cancelled_child_keeps_immutable_registration(self) -> None:
        request = valid_request(self.workflow)
        self.store.admit_request(request)
        self.store.transition_state(request["id"], "registration_pending")
        registration = valid_registration(request)
        self.store.register_child(registration)
        cancelled = self.store.transition_state(request["id"], "cancelled")
        self.assertEqual(cancelled["state"], "cancelled")
        self.assertEqual(self.store.register_child(registration), registration)
        self.assertEqual(self.store.load_state(request["id"])["state"], "cancelled")

    def test_authoritative_records_use_atomic_writes_and_layout_fsync(self) -> None:
        request = valid_request(self.workflow)
        actual_atomic_write = store.atomic_write_text
        actual_fsync = store.fsync_directory

        with (
            mock.patch.object(
                store,
                "atomic_write_text",
                wraps=actual_atomic_write,
            ) as atomic,
            mock.patch.object(
                store,
                "fsync_directory",
                wraps=actual_fsync,
            ) as fsync,
        ):
            self.store.admit_request(request)

        written_paths = {call.args[0] for call in atomic.call_args_list}
        self.assertIn(self.store._request_path(request["id"]), written_paths)
        self.assertIn(self.store._state_path(request["id"]), written_paths)
        self.assertGreaterEqual(fsync.call_count, 1)

    def test_mutations_reuse_workflow_execution_lock(self) -> None:
        request = valid_request(self.workflow)
        with mock.patch.object(
            self.workflow_store,
            "execution_lock",
            wraps=self.workflow_store.execution_lock,
        ) as execution_lock:
            self.store.admit_request(request)

        execution_lock.assert_called_once_with(self.workflow["id"])


if __name__ == "__main__":
    unittest.main()
