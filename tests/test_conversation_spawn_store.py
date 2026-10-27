from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from local_agent.conversation import contract
from local_agent.conversation.spawn_store import WorkflowConversationSpawnStore
from local_agent.conversation.store import WorkflowConversationStore
from local_agent.workflow import contract as workflow_contract
from local_agent.workflow.store import WorkflowStore


BINDING = "12345678-1234-4abc-8def-1234567890ab"
PARENT_URL = "https://chatgpt.com/c/11111111-1111-4111-8111-111111111111"
CHILD_A_URL = "https://chatgpt.com/c/22222222-2222-4222-8222-222222222222"
CHILD_B_URL = "https://chatgpt.com/c/33333333-3333-4333-8333-333333333333"


def workflow(workflow_id: str, node_ids: tuple[str, ...]) -> dict:
    return {
        "schema_version": workflow_contract.WORKFLOW_SCHEMA_VERSION,
        "id": workflow_id,
        "created_at": "2026-09-24T00:00:00Z",
        "nodes": [
            {
                "id": node_id,
                "kind": "reasoning",
                "repository_id": "local-agent",
                "agent_binding": BINDING,
                "depends_on": [],
            }
            for node_id in node_ids
        ],
    }


def request(manifest: dict, node_id: str, request_id: str) -> dict:
    return {
        "schema_version": contract.CHILD_REQUEST_SCHEMA_VERSION,
        "id": request_id,
        "workflow_id": manifest["id"],
        "workflow_node_id": node_id,
        "workflow_node_revision": 0,
        "workflow_node_introduction_digest": workflow_contract.manifest_digest(manifest),
        "parent_conversation_url": PARENT_URL,
        "created_at": "2026-09-24T00:00:10Z",
        "role": "verification",
        "repository_id": "local-agent",
        "agent_binding": BINDING,
        "repository_ref": "main",
        "repository_commit_sha": "a" * 40,
        "scope": {
            "summary": f"Reason about {node_id}.",
            "paths": ["local_agent/conversation"],
        },
        "context_refs": [],
        "bootstrap_contract_version": contract.BOOTSTRAP_CONTRACT_VERSION,
    }


def registration(req: dict, child_url: str) -> dict:
    return {
        "schema_version": contract.CHILD_REGISTRATION_SCHEMA_VERSION,
        "child_request_id": req["id"],
        "child_request_digest": contract.child_request_digest(req),
        "parent_conversation_url": req["parent_conversation_url"],
        "child_conversation_url": child_url,
        "registered_at": "2026-09-24T00:10:00Z",
    }


class SpawnStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state_dir = Path(self.tmp.name)
        self.workflow_store = WorkflowStore(self.state_dir)
        self.manifest = workflow("spawn-a", ("node-a", "node-b"))
        self.workflow_store.submit(self.manifest)
        self.conversations = WorkflowConversationStore(
            self.workflow_store,
            self.manifest["id"],
        )
        self.req_a = request(self.manifest, "node-a", "request-a")
        self.req_b = request(self.manifest, "node-b", "request-b")
        self.conversations.admit_request(self.req_a)
        self.conversations.admit_request(self.req_b)
        self.spawns = WorkflowConversationSpawnStore(self.conversations)

    def enqueue_pair(self) -> tuple[dict, dict]:
        first = self.spawns.enqueue(
            self.req_a["id"],
            created_at="2026-09-24T00:01:00Z",
        )
        second = self.spawns.enqueue(
            self.req_b["id"],
            created_at="2026-09-24T00:02:00Z",
        )
        return first, second

    def advance_to_submitting(self, request_id: str, attempt: int, tab_id: int) -> dict:
        self.spawns.begin_browser_attempt(
            request_id,
            attempt,
            tab_id=tab_id,
            updated_at="2026-09-24T00:03:00Z",
        )
        self.spawns.advance(
            request_id,
            attempt,
            target="bootstrap_ready",
            updated_at="2026-09-24T00:03:01Z",
        )
        return self.spawns.advance(
            request_id,
            attempt,
            target="bootstrap_submitting",
            updated_at="2026-09-24T00:03:02Z",
        )

    def test_enqueue_is_durable_idempotent_and_restart_reconstructable(self) -> None:
        first = self.spawns.enqueue(
            self.req_a["id"],
            created_at="2026-09-24T00:01:00Z",
        )
        retry = self.spawns.enqueue(
            self.req_a["id"],
            created_at="2026-09-24T00:05:00Z",
        )
        self.assertEqual(first, retry)
        self.assertEqual(first["attempt"], 1)
        self.assertEqual(first["state"], "pending")
        self.assertEqual(
            self.conversations.load_state(self.req_a["id"])["state"],
            "registration_pending",
        )

        restarted = WorkflowConversationSpawnStore(
            WorkflowConversationStore(
                WorkflowStore(self.state_dir),
                self.manifest["id"],
            )
        )
        self.assertEqual(restarted.load_attempts(self.req_a["id"]), [first])

    def test_global_queue_owner_is_oldest_pending_request(self) -> None:
        first, second = self.enqueue_pair()
        snapshot = self.spawns.queue_snapshot()
        self.assertEqual(snapshot["pending_owner_id"], first["id"])
        self.assertEqual(snapshot["pending_count"], 2)
        self.assertIsNone(snapshot["active_transaction_id"])

        with self.assertRaisesRegex(ValueError, "queued behind an earlier request"):
            self.spawns.begin_browser_attempt(
                self.req_b["id"],
                second["attempt"],
                tab_id=52,
                updated_at="2026-09-24T00:03:00Z",
            )

    def test_browser_ownership_is_global_and_tab_claim_retry_is_idempotent(self) -> None:
        first, second = self.enqueue_pair()
        active = self.spawns.begin_browser_attempt(
            self.req_a["id"],
            first["attempt"],
            tab_id=41,
            updated_at="2026-09-24T00:03:00Z",
        )
        retry = self.spawns.begin_browser_attempt(
            self.req_a["id"],
            first["attempt"],
            tab_id=41,
            updated_at="2026-09-24T00:03:01Z",
        )
        self.assertEqual(retry, active)
        with self.assertRaisesRegex(ValueError, "original tab"):
            self.spawns.begin_browser_attempt(
                self.req_a["id"],
                first["attempt"],
                tab_id=42,
                updated_at="2026-09-24T00:03:02Z",
            )
        with self.assertRaisesRegex(ValueError, "already active"):
            self.spawns.begin_browser_attempt(
                self.req_b["id"],
                second["attempt"],
                tab_id=52,
                updated_at="2026-09-24T00:03:03Z",
            )

    def test_safe_failure_releases_ownership_and_allows_next_request(self) -> None:
        first, second = self.enqueue_pair()
        self.spawns.begin_browser_attempt(
            self.req_a["id"],
            first["attempt"],
            tab_id=41,
            updated_at="2026-09-24T00:03:00Z",
        )
        failed = self.spawns.fail(
            self.req_a["id"],
            first["attempt"],
            reason="tab closed before bootstrap submission",
            updated_at="2026-09-24T00:03:01Z",
        )
        self.assertEqual(failed["state"], "failed")
        self.assertEqual(
            self.spawns.fail(
                self.req_a["id"],
                first["attempt"],
                reason="tab closed before bootstrap submission",
                updated_at="2026-09-24T00:03:02Z",
            ),
            failed,
        )
        next_active = self.spawns.begin_browser_attempt(
            self.req_b["id"],
            second["attempt"],
            tab_id=52,
            updated_at="2026-09-24T00:03:03Z",
        )
        self.assertEqual(next_active["state"], "tab_created")

    def test_unresolved_ambiguous_blocks_queue_and_replacement_attempt(self) -> None:
        first, second = self.enqueue_pair()
        self.advance_to_submitting(self.req_a["id"], first["attempt"], 41)
        ambiguous = self.spawns.fail(
            self.req_a["id"],
            first["attempt"],
            reason="bootstrap may have been accepted before acknowledgement was lost",
            updated_at="2026-09-24T00:03:03Z",
        )
        self.assertEqual(ambiguous["state"], "ambiguous")
        snapshot = self.spawns.queue_snapshot()
        self.assertEqual(snapshot["unresolved_ambiguous_ids"], [first["id"]])
        self.assertEqual(snapshot["pending_owner_id"], second["id"])

        with self.assertRaisesRegex(ValueError, "ambiguous spawn blocks"):
            self.spawns.begin_browser_attempt(
                self.req_b["id"],
                second["attempt"],
                tab_id=52,
                updated_at="2026-09-24T00:04:00Z",
            )
        with self.assertRaisesRegex(ValueError, "forbids an automatic replacement"):
            self.spawns.enqueue(
                self.req_a["id"],
                created_at="2026-09-24T00:04:01Z",
            )

    def test_abandoned_ambiguous_releases_global_queue_without_replacement(self) -> None:
        first, second = self.enqueue_pair()
        self.advance_to_submitting(self.req_a["id"], first["attempt"], 41)
        ambiguous = self.spawns.fail(
            self.req_a["id"],
            first["attempt"],
            reason="submitted child identity became unrecoverable",
            updated_at="2026-09-24T00:03:03Z",
        )
        self.assertEqual(ambiguous["state"], "ambiguous")
        self.assertIn(ambiguous["id"], self.spawns.queue_snapshot()["unresolved_ambiguous_ids"])

        abandoned = self.spawns.abandon_ambiguous(
            self.req_a["id"],
            first["attempt"],
            updated_at="2026-09-24T00:04:00Z",
        )
        self.assertEqual(abandoned["state"], "abandoned")
        self.assertEqual(self.spawns.load_attempt(self.req_a["id"], 1)["state"], "ambiguous")
        self.assertEqual(len(self.spawns.load_attempts(self.req_a["id"])), 1)
        self.assertNotIn(
            ambiguous["id"],
            self.spawns.queue_snapshot()["unresolved_ambiguous_ids"],
        )
        next_active = self.spawns.begin_browser_attempt(
            self.req_b["id"],
            second["attempt"],
            tab_id=52,
            updated_at="2026-09-24T00:04:01Z",
        )
        self.assertEqual(next_active["state"], "tab_created")
        with self.assertRaisesRegex(ValueError, "requested or registration_pending"):
            self.spawns.enqueue(
                self.req_a["id"],
                created_at="2026-09-24T00:04:02Z",
            )

    def test_manual_registration_resolves_ambiguity_without_replacement(self) -> None:
        first, second = self.enqueue_pair()
        self.advance_to_submitting(self.req_a["id"], first["attempt"], 41)
        self.spawns.fail(
            self.req_a["id"],
            first["attempt"],
            reason="lost acknowledgement",
            updated_at="2026-09-24T00:03:03Z",
        )
        self.conversations.register_child(registration(self.req_a, CHILD_A_URL))

        snapshot = self.spawns.queue_snapshot()
        self.assertEqual(snapshot["unresolved_ambiguous_ids"], [])
        next_active = self.spawns.begin_browser_attempt(
            self.req_b["id"],
            second["attempt"],
            tab_id=52,
            updated_at="2026-09-24T00:04:00Z",
        )
        self.assertEqual(next_active["state"], "tab_created")
        self.assertEqual(len(self.spawns.load_attempts(self.req_a["id"])), 1)

    def test_safe_failed_attempt_can_retry_with_next_attempt_number(self) -> None:
        first = self.spawns.enqueue(
            self.req_a["id"],
            created_at="2026-09-24T00:01:00Z",
        )
        self.spawns.fail(
            self.req_a["id"],
            first["attempt"],
            reason="safe pre-submit failure",
            updated_at="2026-09-24T00:01:01Z",
        )
        second = self.spawns.enqueue(
            self.req_a["id"],
            created_at="2026-09-24T00:01:02Z",
        )
        self.assertEqual(second["attempt"], 2)
        self.assertNotEqual(first["id"], second["id"])
        self.assertEqual(len(self.spawns.load_attempts(self.req_a["id"])), 2)

    def test_identity_discovery_retry_is_idempotent_but_cannot_change_child(self) -> None:
        first = self.spawns.enqueue(
            self.req_a["id"],
            created_at="2026-09-24T00:01:00Z",
        )
        self.advance_to_submitting(self.req_a["id"], first["attempt"], 41)
        discovered = self.spawns.advance(
            self.req_a["id"],
            first["attempt"],
            target="identity_discovered",
            child_conversation_url=CHILD_A_URL,
            updated_at="2026-09-24T00:03:03Z",
        )
        retry = self.spawns.advance(
            self.req_a["id"],
            first["attempt"],
            target="identity_discovered",
            child_conversation_url=CHILD_A_URL,
            updated_at="2026-09-24T00:03:04Z",
        )
        self.assertEqual(retry, discovered)
        with self.assertRaisesRegex(ValueError, "conflicts with durable child URL"):
            self.spawns.advance(
                self.req_a["id"],
                first["attempt"],
                target="identity_discovered",
                child_conversation_url=CHILD_B_URL,
                updated_at="2026-09-24T00:03:05Z",
            )

    def test_registration_crash_recovery_converges_to_done_without_new_spawn(self) -> None:
        first = self.spawns.enqueue(
            self.req_a["id"],
            created_at="2026-09-24T00:01:00Z",
        )
        self.advance_to_submitting(self.req_a["id"], first["attempt"], 41)
        self.spawns.advance(
            self.req_a["id"],
            first["attempt"],
            target="identity_discovered",
            child_conversation_url=CHILD_A_URL,
            updated_at="2026-09-24T00:03:03Z",
        )
        self.spawns.advance(
            self.req_a["id"],
            first["attempt"],
            target="registration_submitting",
            updated_at="2026-09-24T00:03:04Z",
        )
        self.conversations.register_child(registration(self.req_a, CHILD_A_URL))

        restarted = WorkflowConversationSpawnStore(
            WorkflowConversationStore(
                WorkflowStore(self.state_dir),
                self.manifest["id"],
            )
        )
        recovered = restarted.reconcile_registration(
            self.req_a["id"],
            first["attempt"],
            updated_at="2026-09-24T00:10:01Z",
        )
        self.assertEqual(recovered["state"], "done")
        self.assertEqual(len(restarted.load_attempts(self.req_a["id"])), 1)
        self.assertEqual(
            restarted.reconcile_registration(
                self.req_a["id"],
                first["attempt"],
                updated_at="2026-09-24T00:10:02Z",
            ),
            recovered,
        )

    def test_registration_must_match_discovered_child_url(self) -> None:
        first = self.spawns.enqueue(
            self.req_a["id"],
            created_at="2026-09-24T00:01:00Z",
        )
        self.advance_to_submitting(self.req_a["id"], first["attempt"], 41)
        self.spawns.advance(
            self.req_a["id"],
            first["attempt"],
            target="identity_discovered",
            child_conversation_url=CHILD_A_URL,
            updated_at="2026-09-24T00:03:03Z",
        )
        self.spawns.fail(
            self.req_a["id"],
            first["attempt"],
            reason="lost registration acknowledgement",
            updated_at="2026-09-24T00:03:04Z",
        )
        self.conversations.register_child(registration(self.req_a, CHILD_B_URL))
        with self.assertRaisesRegex(ValueError, "conflicts with spawn-discovered"):
            self.spawns.queue_snapshot()

    def test_restart_reconstructs_active_queue_without_active_pointer(self) -> None:
        first, _second = self.enqueue_pair()
        self.spawns.begin_browser_attempt(
            self.req_a["id"],
            first["attempt"],
            tab_id=41,
            updated_at="2026-09-24T00:03:00Z",
        )
        self.assertFalse((self.state_dir / "conversation-spawn-active.json").exists())

        restarted = WorkflowConversationSpawnStore(
            WorkflowConversationStore(
                WorkflowStore(self.state_dir),
                self.manifest["id"],
            )
        )
        snapshot = restarted.queue_snapshot()
        self.assertEqual(snapshot["active_transaction_id"], first["id"])
        self.assertEqual(snapshot["pending_count"], 1)

    def test_global_serialization_spans_workflows(self) -> None:
        first = self.spawns.enqueue(
            self.req_a["id"],
            created_at="2026-09-24T00:01:00Z",
        )

        other_manifest = workflow("spawn-b", ("node-c",))
        self.workflow_store.submit(other_manifest)
        other_conversations = WorkflowConversationStore(
            self.workflow_store,
            other_manifest["id"],
        )
        other_request = request(other_manifest, "node-c", "request-c")
        other_conversations.admit_request(other_request)
        other_spawns = WorkflowConversationSpawnStore(other_conversations)
        second = other_spawns.enqueue(
            other_request["id"],
            created_at="2026-09-24T00:02:00Z",
        )

        with self.assertRaisesRegex(ValueError, "queued behind an earlier request"):
            other_spawns.begin_browser_attempt(
                other_request["id"],
                second["attempt"],
                tab_id=63,
                updated_at="2026-09-24T00:03:00Z",
            )
        active = self.spawns.begin_browser_attempt(
            self.req_a["id"],
            first["attempt"],
            tab_id=41,
            updated_at="2026-09-24T00:03:01Z",
        )
        self.assertEqual(active["state"], "tab_created")
        self.assertEqual(
            other_spawns.queue_snapshot()["active_transaction_id"],
            first["id"],
        )


if __name__ == "__main__":
    unittest.main()
