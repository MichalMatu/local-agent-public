from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from local_agent.conversation.store import WorkflowConversationStore
from local_agent.development.lab import build_dev_lab_layout, initialize_dev_lab
from local_agent.development.live_seed import (
    LIVE_SEED_NODE_ID,
    LIVE_SEED_REQUEST_ID,
    LIVE_SEED_WORKFLOW_ID,
    inspect_live_seed_checkout,
    seed_live_slice,
)
from local_agent.workflow.store import WorkflowStore


BINDING = "b0e94f93-478f-4fc6-abed-159d3702362f"
PARENT_URL = "https://chatgpt.com/c/11111111-1111-4111-8111-111111111111"
OTHER_PARENT_URL = "https://chatgpt.com/c/22222222-2222-4222-8222-222222222222"


class DevelopmentLiveSeedTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.layout = build_dev_lab_layout(home=self.home)
        self.checkout = self.layout.checkout
        (self.checkout / "chat_bridge").mkdir(parents=True)
        (self.checkout / "config").mkdir(parents=True)
        (self.checkout / "chat_bridge" / "manifest.json").write_text("{}\n", encoding="utf-8")
        self._write_catalog(execution_enabled=False)
        self._git("init", "-b", "work/conversation-live-slice")
        self._git("config", "user.name", "Stage8 Test")
        self._git("config", "user.email", "stage8@example.invalid")
        self._git("remote", "add", "origin", "https://github.com/example-owner/local-agent.git")
        self._git("add", "chat_bridge/manifest.json", "config/agent_bindings.json")
        self._git("commit", "-m", "seed fixture")
        initialize_dev_lab(self.layout)
        self.now = datetime(2026, 9, 24, 20, 45, tzinfo=timezone.utc)

    def _git(self, *args: str) -> str:
        completed = subprocess.run(
            ["git", "-C", str(self.checkout), *args],
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        return completed.stdout.strip()

    def _write_catalog(self, *, execution_enabled: bool) -> None:
        payload = {
            "version": 1,
            "agents": [
                {
                    "id": "local-agent",
                    "repository": "example-owner/local-agent",
                    "agent_binding": BINDING,
                    "execution_enabled": execution_enabled,
                }
            ],
        }
        (self.checkout / "config" / "agent_bindings.json").write_text(
            json.dumps(payload, indent=2) + "\n",
            encoding="utf-8",
        )

    def seed(self, parent_url: str = PARENT_URL) -> dict:
        return seed_live_slice(
            self.layout,
            parent_conversation_url=parent_url,
            now=self.now,
        )

    def test_seed_derives_exact_non_executing_git_and_binding_identity(self) -> None:
        identity = inspect_live_seed_checkout(self.layout)
        head = self._git("rev-parse", "HEAD")

        result = self.seed()

        self.assertEqual(result["workflow_id"], LIVE_SEED_WORKFLOW_ID)
        self.assertEqual(result["workflow_node_id"], LIVE_SEED_NODE_ID)
        self.assertEqual(result["request_id"], LIVE_SEED_REQUEST_ID)
        self.assertEqual(result["parent_conversation_url"], PARENT_URL)
        self.assertEqual(result["repository"], "example-owner/local-agent")
        self.assertEqual(result["repository_id"], "local-agent")
        self.assertEqual(result["agent_binding"], BINDING)
        self.assertEqual(result["repository_ref"], "work/conversation-live-slice")
        self.assertEqual(result["repository_commit_sha"], head)
        self.assertFalse(result["execution_enabled"])
        self.assertEqual(identity.repository_commit_sha, head)
        self.assertEqual(result["workflow_state"], "waiting_conversation")
        self.assertEqual(result["child_state"], "requested")
        self.assertTrue(result["created_workflow"])
        self.assertTrue(result["created_request"])

        workflow_store = WorkflowStore(self.layout.state_dir)
        manifest = workflow_store.load_manifest(LIVE_SEED_WORKFLOW_ID)
        self.assertEqual(manifest["nodes"][0]["kind"], "reasoning")
        self.assertNotIn("task", manifest["nodes"][0])
        conversations = WorkflowConversationStore(workflow_store, LIVE_SEED_WORKFLOW_ID)
        request = conversations.load_request(LIVE_SEED_REQUEST_ID)
        self.assertEqual(request["repository_ref"], "work/conversation-live-slice")
        self.assertEqual(request["repository_commit_sha"], head)
        self.assertIn("Do not request or queue Local Agent execution", request["scope"]["summary"])

    def test_seed_is_idempotent_for_same_parent_and_checkout(self) -> None:
        first = self.seed()
        second = self.seed()

        self.assertEqual(first["request_digest"], second["request_digest"])
        self.assertTrue(first["created_workflow"])
        self.assertTrue(first["created_request"])
        self.assertFalse(second["created_workflow"])
        self.assertFalse(second["created_request"])

    def test_existing_seed_refuses_a_different_parent(self) -> None:
        self.seed()

        with self.assertRaisesRegex(RuntimeError, "conflicts with parent URL"):
            self.seed(OTHER_PARENT_URL)

    def test_existing_seed_refuses_moved_checkout_head(self) -> None:
        self.seed()
        (self.checkout / "next.txt").write_text("next\n", encoding="utf-8")
        self._git("add", "next.txt")
        self._git("commit", "-m", "move head")

        with self.assertRaisesRegex(RuntimeError, "current DEV checkout"):
            self.seed()

    def test_dirty_checkout_is_rejected_before_persistence(self) -> None:
        (self.checkout / "dirty.txt").write_text("dirty\n", encoding="utf-8")

        with self.assertRaisesRegex(RuntimeError, "must be clean"):
            self.seed()

        self.assertNotIn(LIVE_SEED_WORKFLOW_ID, WorkflowStore(self.layout.state_dir).workflow_ids())

    def test_main_branch_is_rejected(self) -> None:
        self._git("checkout", "-b", "main")

        with self.assertRaisesRegex(RuntimeError, "requires develop/conversation-fabric"):
            self.seed()

    def test_wrong_origin_repository_is_rejected(self) -> None:
        self._git("remote", "set-url", "origin", "https://github.com/example-owner/not-local-agent.git")

        with self.assertRaisesRegex(RuntimeError, "origin repository mismatch"):
            self.seed()

    def test_execution_enabled_catalog_entry_is_rejected(self) -> None:
        self._write_catalog(execution_enabled=True)
        self._git("add", "config/agent_bindings.json")
        self._git("commit", "-m", "unsafe catalog")

        with self.assertRaisesRegex(RuntimeError, "execution_enabled=false"):
            self.seed()

    def test_parent_url_must_already_be_canonical(self) -> None:
        with self.assertRaisesRegex(ValueError, "canonical"):
            self.seed("https://chat.openai.com/c/11111111-1111-4111-8111-111111111111")


if __name__ == "__main__":
    unittest.main()
