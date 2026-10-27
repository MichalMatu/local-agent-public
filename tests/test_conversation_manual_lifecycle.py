from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from local_agent.conversation import adoption, bootstrap, contract, terminal
from local_agent.conversation.spawn_store import WorkflowConversationSpawnStore
from local_agent.conversation.store import WorkflowConversationStore
from local_agent.development.lab import build_dev_lab_layout, initialize_dev_lab
from local_agent.development.manual_lifecycle import attach_manual_child, prepare_manual_child
from local_agent.workflow import contract as workflow_contract
from local_agent.workflow.store import WorkflowStore


BINDING = "12345678-1234-4abc-8def-1234567890ab"
WORKFLOW_ID = "manual-lifecycle-workflow"
NODE_ID = "manual-reasoning-node"
REQUEST_ID = "manual-child-001"
PARENT_URL = "https://chatgpt.com/c/11111111-1111-4111-8111-111111111111"
CHILD_URL = "https://chatgpt.com/c/22222222-2222-4222-8222-222222222222"
OTHER_CHILD_URL = "https://chatgpt.com/c/33333333-3333-4333-8333-333333333333"


def workflow_manifest() -> dict:
    return {
        "schema_version": workflow_contract.WORKFLOW_SCHEMA_VERSION,
        "id": WORKFLOW_ID,
        "created_at": "2026-10-02T18:00:00Z",
        "nodes": [
            {
                "id": NODE_ID,
                "kind": "reasoning",
                "repository_id": "local-agent",
                "agent_binding": BINDING,
                "depends_on": [],
            }
        ],
    }


def child_request(manifest: dict) -> dict:
    return {
        "schema_version": contract.CHILD_REQUEST_SCHEMA_VERSION,
        "id": REQUEST_ID,
        "workflow_id": WORKFLOW_ID,
        "workflow_node_id": NODE_ID,
        "workflow_node_revision": 0,
        "workflow_node_introduction_digest": workflow_contract.manifest_digest(manifest),
        "parent_conversation_url": PARENT_URL,
        "created_at": "2026-10-02T18:01:00Z",
        "role": "verification",
        "repository_id": "local-agent",
        "agent_binding": BINDING,
        "repository_ref": "develop/conversation-fabric",
        "repository_commit_sha": "a" * 40,
        "scope": {
            "summary": "Prove one bounded manual Conversation Fabric lifecycle.",
            "paths": ["local_agent/conversation"],
        },
        "context_refs": [],
        "bootstrap_contract_version": contract.BOOTSTRAP_CONTRACT_VERSION,
    }


class ConversationManualLifecycleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.layout = build_dev_lab_layout(home=self.home)
        extension = self.layout.checkout / "chat_bridge"
        extension.mkdir(parents=True)
        (extension / "manifest.json").write_text("{}\n", encoding="utf-8")
        initialize_dev_lab(self.layout)

        self.workflow_store = WorkflowStore(self.layout.state_dir)
        self.manifest = workflow_manifest()
        self.workflow_store.submit(self.manifest)
        self.conversations = WorkflowConversationStore(self.workflow_store, WORKFLOW_ID)
        self.request = child_request(self.manifest)
        self.conversations.admit_request(self.request)
        self.spawns = WorkflowConversationSpawnStore(self.conversations)
        self.base_time = datetime(2026, 10, 2, 18, 30, tzinfo=timezone.utc)

    def restarted_stores(
        self,
    ) -> tuple[WorkflowStore, WorkflowConversationStore, WorkflowConversationSpawnStore]:
        workflow_store = WorkflowStore(self.layout.state_dir)
        conversations = WorkflowConversationStore(workflow_store, WORKFLOW_ID)
        return workflow_store, conversations, WorkflowConversationSpawnStore(conversations)

    def test_prepare_is_read_only_and_deterministic(self) -> None:
        first = prepare_manual_child(self.layout, WORKFLOW_ID, REQUEST_ID)
        second = prepare_manual_child(self.layout, WORKFLOW_ID, REQUEST_ID)

        self.assertEqual(first, second)
        self.assertEqual(first["request_digest"], contract.child_request_digest(self.request))
        self.assertEqual(first["bootstrap_text"], bootstrap.child_bootstrap_message(self.request))
        self.assertEqual(first["bootstrap_digest"], bootstrap.child_bootstrap_digest(self.request))
        self.assertEqual(first["child_state"], "requested")
        self.assertEqual(first["spawn_attempt_count"], 0)
        self.assertEqual(self.conversations.load_state(REQUEST_ID)["state"], "requested")
        self.assertIsNone(self.conversations.load_registration(REQUEST_ID))
        self.assertEqual(self.spawns.load_attempts(REQUEST_ID), [])

    def test_manual_attach_registers_without_spawn_and_retries_idempotently(self) -> None:
        prepare_manual_child(self.layout, WORKFLOW_ID, REQUEST_ID)
        first = attach_manual_child(
            self.layout,
            WORKFLOW_ID,
            REQUEST_ID,
            child_conversation_url=CHILD_URL,
            now=self.base_time,
        )

        self.assertEqual(first["status"], "completed")
        self.assertEqual(first["resolution"], "manual_attach")
        self.assertEqual(first["child_conversation_url"], CHILD_URL)
        self.assertEqual(first["child_state"], "active")
        self.assertEqual(first["spawn_attempt_count"], 0)
        self.assertEqual(self.spawns.load_attempts(REQUEST_ID), [])
        registration = self.conversations.load_registration(REQUEST_ID)
        self.assertIsNotNone(registration)
        self.assertEqual(registration["child_conversation_url"], CHILD_URL)

        _, restarted, restarted_spawns = self.restarted_stores()
        retried = restarted_spawns.attach_manual_child(
            REQUEST_ID,
            child_conversation_url=CHILD_URL,
            registered_at=(self.base_time + timedelta(minutes=1))
            .isoformat()
            .replace("+00:00", "Z"),
        )
        self.assertEqual(retried, registration)
        self.assertEqual(restarted.load_state(REQUEST_ID)["state"], "active")
        self.assertEqual(restarted_spawns.load_attempts(REQUEST_ID), [])

    def test_manual_attach_conflict_fails_closed(self) -> None:
        self.spawns.attach_manual_child(
            REQUEST_ID,
            child_conversation_url=CHILD_URL,
            registered_at="2026-10-02T18:30:00Z",
        )

        with self.assertRaisesRegex(ValueError, "conflicting child conversation URL"):
            self.spawns.attach_manual_child(
                REQUEST_ID,
                child_conversation_url=OTHER_CHILD_URL,
                registered_at="2026-10-02T18:31:00Z",
            )

        self.assertEqual(
            self.conversations.load_registration(REQUEST_ID)["child_conversation_url"],
            CHILD_URL,
        )
        self.assertEqual(self.spawns.load_attempts(REQUEST_ID), [])

    def test_manual_attach_rejects_any_existing_spawn_attempt(self) -> None:
        transaction = self.spawns.enqueue(
            REQUEST_ID,
            created_at="2026-10-02T18:20:00Z",
        )
        self.assertEqual(transaction["state"], "pending")

        with self.assertRaisesRegex(RuntimeError, "zero spawn attempts"):
            prepare_manual_child(self.layout, WORKFLOW_ID, REQUEST_ID)
        with self.assertRaisesRegex(ValueError, "zero spawn attempts"):
            self.spawns.attach_manual_child(
                REQUEST_ID,
                child_conversation_url=CHILD_URL,
                registered_at="2026-10-02T18:30:00Z",
            )

        self.assertIsNone(self.conversations.load_registration(REQUEST_ID))
        self.assertEqual(len(self.spawns.load_attempts(REQUEST_ID)), 1)

    def test_manual_attach_restart_after_pending_transition_is_safe(self) -> None:
        with mock.patch.object(
            self.conversations,
            "register_child",
            side_effect=OSError("simulated interruption before registration write"),
        ):
            with self.assertRaisesRegex(OSError, "simulated interruption"):
                self.spawns.attach_manual_child(
                    REQUEST_ID,
                    child_conversation_url=CHILD_URL,
                    registered_at="2026-10-02T18:30:00Z",
                )

        self.assertEqual(
            self.conversations.load_state(REQUEST_ID)["state"],
            "registration_pending",
        )
        self.assertIsNone(self.conversations.load_registration(REQUEST_ID))
        self.assertEqual(self.spawns.load_attempts(REQUEST_ID), [])

        _, restarted, restarted_spawns = self.restarted_stores()
        registration = restarted_spawns.attach_manual_child(
            REQUEST_ID,
            child_conversation_url=CHILD_URL,
            registered_at="2026-10-02T18:31:00Z",
        )
        self.assertEqual(registration["child_conversation_url"], CHILD_URL)
        self.assertEqual(restarted.load_state(REQUEST_ID)["state"], "active")

    def test_manually_attached_child_uses_normal_terminal_adoption_retirement(self) -> None:
        attach_manual_child(
            self.layout,
            WORKFLOW_ID,
            REQUEST_ID,
            child_conversation_url=CHILD_URL,
            now=self.base_time,
        )

        workflow_store, conversations, spawns = self.restarted_stores()
        self.assertEqual(spawns.load_attempts(REQUEST_ID), [])
        conversations.transition_state(REQUEST_ID, "terminal_pending_evidence")
        registration = conversations.load_registration(REQUEST_ID)
        self.assertIsNotNone(registration)

        terminal_record = {
            "schema_version": terminal.TERMINAL_RECORD_SCHEMA_VERSION,
            "child_request_id": REQUEST_ID,
            "child_request_digest": contract.child_request_digest(self.request),
            "child_registration_digest": terminal.child_registration_digest(
                registration,
                request=self.request,
            ),
            "child_conversation_url": CHILD_URL,
            "summary": "Manual child completed the bounded reasoning scope.",
            "evidence_refs": [
                {
                    "kind": "finding",
                    "id": "manual-finding-001",
                    "digest": "sha256:" + "1" * 64,
                }
            ],
            "recorded_at": "2026-10-02T18:40:00Z",
        }
        conversations.record_terminal(terminal_record)
        self.assertEqual(conversations.load_state(REQUEST_ID)["state"], "terminal_recorded")

        durable_terminal = conversations.load_terminal_record(REQUEST_ID)
        self.assertIsNotNone(durable_terminal)
        adoption_record = {
            "schema_version": adoption.ADOPTION_RECORD_SCHEMA_VERSION,
            "child_request_id": REQUEST_ID,
            "child_request_digest": contract.child_request_digest(self.request),
            "terminal_record_digest": adoption.terminal_record_digest(
                durable_terminal,
                request=self.request,
                registration=registration,
            ),
            "workflow_id": WORKFLOW_ID,
            "workflow_node_id": NODE_ID,
            "adopted_at": "2026-10-02T18:41:00Z",
        }
        conversations.record_adoption(adoption_record)
        self.assertEqual(
            workflow_store.load_state(WORKFLOW_ID)["node_states"][NODE_ID],
            "succeeded",
        )

        conversations.transition_state(REQUEST_ID, "retired")
        self.assertEqual(conversations.load_state(REQUEST_ID)["state"], "retired")
        self.assertEqual(spawns.load_attempts(REQUEST_ID), [])


if __name__ == "__main__":
    unittest.main()
