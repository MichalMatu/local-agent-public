from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from local_agent.conversation import contract
from local_agent.conversation.spawn_store import WorkflowConversationSpawnStore
from local_agent.conversation.store import WorkflowConversationStore
from local_agent.development.lab import build_dev_lab_layout, initialize_dev_lab
from local_agent.development.live_runner import (
    _write_journal,
    run_live_slice,
    runner_paths,
)
from local_agent.development.live_slice import (
    arm_live_slice,
    live_slice_paths,
    prepare_live_slice,
)
from local_agent.workflow.store import WorkflowStore
from tests.test_development_live_runner import (
    CHILD_URL,
    PARENT_URL,
    REQUEST_ID,
    WORKFLOW_ID,
    child_request,
    workflow_manifest,
)


class ConversationRestartRecoveryTests(unittest.TestCase):
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
        self.base_time = datetime(2026, 9, 24, 18, 30, tzinfo=timezone.utc)

    def now(self) -> datetime:
        return self.base_time

    def prepare_and_arm(self) -> dict[str, Any]:
        prepared = prepare_live_slice(
            self.layout,
            WORKFLOW_ID,
            REQUEST_ID,
            created_at="2026-09-24T18:10:00Z",
        )
        arm = arm_live_slice(
            self.layout,
            expected_plan_digest=prepared["plan_digest"],
            ttl_seconds=300,
            now=self.base_time,
        )
        return {"prepared": prepared, "arm": arm}

    def advance_to_identity_discovered(self) -> None:
        self.spawns.begin_browser_attempt(
            REQUEST_ID,
            1,
            tab_id=91,
            updated_at="2026-09-24T18:20:00Z",
        )
        self.spawns.advance(
            REQUEST_ID,
            1,
            target="bootstrap_ready",
            updated_at="2026-09-24T18:21:00Z",
        )
        self.spawns.advance(
            REQUEST_ID,
            1,
            target="bootstrap_submitting",
            updated_at="2026-09-24T18:22:00Z",
        )
        self.spawns.advance(
            REQUEST_ID,
            1,
            target="identity_discovered",
            child_conversation_url=CHILD_URL,
            updated_at="2026-09-24T18:23:00Z",
        )

    def rearm_after_crash(self, prepared: dict[str, Any]) -> dict[str, Any]:
        live_slice_paths(self.layout).arm.unlink()
        return arm_live_slice(
            self.layout,
            expected_plan_digest=prepared["plan_digest"],
            ttl_seconds=300,
            now=self.base_time + timedelta(seconds=1),
        )

    def browser_must_not_start(self, _layout: Any) -> Any:
        self.fail("browser must not start after durable identity discovery")

    def test_identity_discovered_restart_advances_registration_without_browser(self) -> None:
        armed = self.prepare_and_arm()
        self.advance_to_identity_discovered()
        _write_journal(
            self.layout,
            document=armed["prepared"],
            phase="identity_persisted",
            now=self.base_time,
        )
        rearm = self.rearm_after_crash(armed["prepared"])

        result = run_live_slice(
            self.layout,
            launch_nonce=rearm["launch_nonce"],
            session_factory=self.browser_must_not_start,
            now=self.now,
        )

        self.assertEqual(result["status"], "completed")
        self.assertFalse(result["recovered"])
        self.assertEqual(self.spawns.load_attempt(REQUEST_ID, 1)["state"], "done")
        registration = self.conversations.load_registration(REQUEST_ID)
        self.assertIsNotNone(registration)
        self.assertEqual(registration["child_conversation_url"], CHILD_URL)
        self.assertTrue(runner_paths(self.layout).evidence.is_file())

    def test_registration_submitting_restart_writes_registration_without_browser(self) -> None:
        armed = self.prepare_and_arm()
        self.advance_to_identity_discovered()
        self.spawns.advance(
            REQUEST_ID,
            1,
            target="registration_submitting",
            updated_at="2026-09-24T18:24:00Z",
        )
        _write_journal(
            self.layout,
            document=armed["prepared"],
            phase="registration_started",
            now=self.base_time,
        )
        self.assertIsNone(self.conversations.load_registration(REQUEST_ID))
        rearm = self.rearm_after_crash(armed["prepared"])

        result = run_live_slice(
            self.layout,
            launch_nonce=rearm["launch_nonce"],
            session_factory=self.browser_must_not_start,
            now=self.now,
        )

        self.assertEqual(result["status"], "completed")
        self.assertFalse(result["recovered"])
        self.assertEqual(self.spawns.load_attempt(REQUEST_ID, 1)["state"], "done")
        registration = self.conversations.load_registration(REQUEST_ID)
        self.assertIsNotNone(registration)
        self.assertEqual(registration["child_conversation_url"], CHILD_URL)
        self.assertTrue(runner_paths(self.layout).evidence.is_file())

    def test_done_restart_materializes_completion_evidence_without_arm_or_browser(self) -> None:
        armed = self.prepare_and_arm()
        self.advance_to_identity_discovered()
        self.spawns.advance(
            REQUEST_ID,
            1,
            target="registration_submitting",
            updated_at="2026-09-24T18:24:00Z",
        )
        _write_journal(
            self.layout,
            document=armed["prepared"],
            phase="registration_started",
            now=self.base_time,
        )
        self.conversations.register_child(
            {
                "schema_version": contract.CHILD_REGISTRATION_SCHEMA_VERSION,
                "child_request_id": REQUEST_ID,
                "child_request_digest": contract.child_request_digest(self.request),
                "parent_conversation_url": PARENT_URL,
                "child_conversation_url": CHILD_URL,
                "registered_at": "2026-09-24T18:25:00Z",
            }
        )
        self.spawns.advance(
            REQUEST_ID,
            1,
            target="done",
            updated_at="2026-09-24T18:26:00Z",
        )
        live_slice_paths(self.layout).arm.unlink()
        self.assertFalse(runner_paths(self.layout).evidence.exists())

        result = run_live_slice(
            self.layout,
            launch_nonce="already-consumed",
            session_factory=self.browser_must_not_start,
            now=self.now,
        )

        self.assertEqual(result["status"], "completed")
        self.assertTrue(result["recovered"])
        self.assertEqual(self.spawns.load_attempt(REQUEST_ID, 1)["state"], "done")
        self.assertTrue(runner_paths(self.layout).evidence.is_file())
        self.assertFalse(runner_paths(self.layout).journal.exists())
        self.assertFalse(live_slice_paths(self.layout).plan.exists())
        self.assertFalse(live_slice_paths(self.layout).arm.exists())


if __name__ == "__main__":
    unittest.main()
