from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from unittest.mock import patch

from local_agent.conversation import contract
from local_agent.conversation.spawn_store import WorkflowConversationSpawnStore
from local_agent.conversation.store import WorkflowConversationStore
from local_agent.development.lab import build_dev_lab_layout, initialize_dev_lab
from local_agent.development.live_runner import (
    _browser_environment,
    _write_journal,
    abandon_ambiguous_live_slice,
    attach_ambiguous_live_slice,
    login_live_slice_browser,
    load_runner_journal,
    run_live_slice,
    runner_paths,
)
from local_agent.development.live_slice import (
    arm_live_slice,
    live_slice_paths,
    prepare_live_slice,
)
from local_agent.workflow import contract as workflow_contract
from local_agent.workflow.store import WorkflowStore


BINDING = "12345678-1234-4abc-8def-1234567890ab"
PARENT_URL = "https://chatgpt.com/c/11111111-1111-4111-8111-111111111111"
CHILD_URL = "https://chatgpt.com/c/22222222-2222-4222-8222-222222222222"
WORKFLOW_ID = "live-runner-workflow"
NODE_ID = "reasoning-node"
REQUEST_ID = "live-runner-child"


def workflow_manifest() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "id": WORKFLOW_ID,
        "created_at": "2026-09-24T18:00:00Z",
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


def child_request(manifest: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": contract.CHILD_REQUEST_SCHEMA_VERSION,
        "id": REQUEST_ID,
        "workflow_id": WORKFLOW_ID,
        "workflow_node_id": NODE_ID,
        "workflow_node_revision": 0,
        "workflow_node_introduction_digest": workflow_contract.manifest_digest(manifest),
        "parent_conversation_url": PARENT_URL,
        "created_at": "2026-09-24T18:01:00Z",
        "role": "verification",
        "repository_id": "local-agent",
        "agent_binding": BINDING,
        "repository_ref": "develop/conversation-fabric",
        "repository_commit_sha": "a" * 40,
        "scope": {
            "summary": "Run one bounded live runner proof.",
            "paths": ["local_agent/conversation"],
        },
        "context_refs": [],
        "bootstrap_contract_version": contract.BOOTSTRAP_CONTRACT_VERSION,
    }


class FakeBrowserSession:
    def __init__(
        self,
        *,
        ready: dict[str, Any] | None = None,
        create: dict[str, Any] | None = None,
        recover_create: dict[str, Any] | None = None,
        reattach_pre_submit: dict[str, Any] | None = None,
        probe: dict[str, Any] | None = None,
        submit: dict[str, Any] | None = None,
        reconcile: dict[str, Any] | None = None,
    ) -> None:
        self.responses = {
            "wait_ready": ready or {"ok": True, "reason": "chatgpt_ready"},
            "create": create or {"ok": True, "reason": "tab_created", "tabId": 41},
            "recover_create": recover_create
            or {"ok": True, "reason": "tab_recovered", "tabId": 41},
            "reattach_pre_submit": reattach_pre_submit
            or {"ok": True, "reason": "tab_reattached", "tabId": 77},
            "probe": probe or {"ok": True, "reason": "spawn_ready"},
            "submit": submit
            or {
                "ok": True,
                "reason": "identity_discovered",
                "childConversationUrl": CHILD_URL,
            },
            "reconcile": reconcile
            or {
                "ok": True,
                "reason": "identity_discovered",
                "childConversationUrl": CHILD_URL,
            },
        }
        self.calls: list[str] = []
        self.wait_timeouts: list[int] = []

    def wait_ready(self, *, timeout_seconds: int) -> dict[str, Any]:
        self.assert_timeout(timeout_seconds)
        self.wait_timeouts.append(timeout_seconds)
        self.calls.append("wait_ready")
        return dict(self.responses["wait_ready"])

    @staticmethod
    def assert_timeout(timeout_seconds: int) -> None:
        if timeout_seconds < 1:
            raise AssertionError("timeout must be positive")

    def create(self, intent: dict[str, Any]) -> dict[str, Any]:
        self.calls.append("create")
        self._assert_intent(intent)
        return dict(self.responses["create"])

    def recover_create(self, intent: dict[str, Any]) -> dict[str, Any]:
        self.calls.append("recover_create")
        self._assert_intent(intent)
        return dict(self.responses["recover_create"])

    def reattach_pre_submit(self, intent: dict[str, Any]) -> dict[str, Any]:
        self.calls.append("reattach_pre_submit")
        self._assert_intent(intent, require_tab=True)
        return dict(self.responses["reattach_pre_submit"])

    def probe(self, intent: dict[str, Any]) -> dict[str, Any]:
        self.calls.append("probe")
        self._assert_intent(intent, require_tab=True)
        return dict(self.responses["probe"])

    def submit(self, intent: dict[str, Any]) -> dict[str, Any]:
        self.calls.append("submit")
        self._assert_intent(intent, require_tab=True)
        return dict(self.responses["submit"])

    def reconcile(self, intent: dict[str, Any]) -> dict[str, Any]:
        self.calls.append("reconcile")
        self._assert_intent(intent, require_tab=True)
        return dict(self.responses["reconcile"])

    def close(self) -> None:
        self.calls.append("close")

    @staticmethod
    def _assert_intent(intent: dict[str, Any], *, require_tab: bool = False) -> None:
        if not str(intent.get("transaction_id", "")).startswith("spawn-"):
            raise AssertionError("missing spawn transaction identity")
        if require_tab and not isinstance(intent.get("tab_id"), int):
            raise AssertionError("browser action requires durable tab id")


class DevelopmentLiveRunnerTests(unittest.TestCase):
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

    def prepare_and_arm(self, *, now: datetime | None = None) -> dict[str, Any]:
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
            now=now or self.base_time,
        )
        return {"prepared": prepared, "arm": arm}

    def run_with(self, session: FakeBrowserSession, nonce: str) -> dict[str, Any]:
        return run_live_slice(
            self.layout,
            launch_nonce=nonce,
            session_factory=lambda _layout: session,
            now=self.now,
        )

    def test_success_persists_registration_before_done_and_writes_evidence(self) -> None:
        armed = self.prepare_and_arm()
        session = FakeBrowserSession()

        result = self.run_with(session, armed["arm"]["launch_nonce"])

        self.assertEqual(result["status"], "completed")
        self.assertFalse(result["recovered"])
        self.assertEqual(result["child_conversation_url"], CHILD_URL)
        self.assertEqual(self.spawns.load_attempt(REQUEST_ID, 1)["state"], "done")
        self.assertEqual(
            self.conversations.load_registration(REQUEST_ID)["child_conversation_url"],
            CHILD_URL,
        )
        self.assertEqual(self.conversations.load_state(REQUEST_ID)["state"], "active")
        self.assertEqual(
            session.calls,
            ["wait_ready", "create", "probe", "submit", "close"],
        )
        self.assertFalse(live_slice_paths(self.layout).plan.exists())
        self.assertFalse(live_slice_paths(self.layout).arm.exists())
        self.assertFalse(runner_paths(self.layout).journal.exists())
        self.assertTrue(runner_paths(self.layout).evidence.is_file())

    def test_browser_environment_auto_discovers_shared_dev_tooling(self) -> None:
        deps = (
            self.home
            / "Library"
            / "Application Support"
            / "local-agent-dev"
            / "node-deps"
        )
        playwright = deps / "node_modules" / "playwright"
        playwright.mkdir(parents=True)
        older = (
            deps
            / "chrome-for-testing-153.0.1.2"
            / "chrome-mac-arm64"
            / "Google Chrome for Testing.app"
            / "Contents"
            / "MacOS"
            / "Google Chrome for Testing"
        )
        newer = (
            deps
            / "chrome-for-testing-154.0.8037.92"
            / "chrome-mac-arm64"
            / "Google Chrome for Testing.app"
            / "Contents"
            / "MacOS"
            / "Google Chrome for Testing"
        )
        for executable in (older, newer):
            executable.parent.mkdir(parents=True)
            executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            executable.chmod(0o755)

        with patch.dict(
            "os.environ",
            {
                "LOCAL_AGENT_PLAYWRIGHT_MODULE": "",
                "LOCAL_AGENT_CHROME_EXECUTABLE": "",
            },
            clear=False,
        ), patch("local_agent.development.live_runner.sys.platform", "darwin"):
            env = _browser_environment(self.layout)

        self.assertEqual(env["LOCAL_AGENT_PLAYWRIGHT_MODULE"], str(playwright.resolve()))
        self.assertEqual(env["LOCAL_AGENT_CHROME_EXECUTABLE"], str(newer.resolve()))

    def test_browser_environment_preserves_explicit_overrides(self) -> None:
        with patch.dict(
            "os.environ",
            {
                "LOCAL_AGENT_PLAYWRIGHT_MODULE": "/explicit/playwright",
                "LOCAL_AGENT_CHROME_EXECUTABLE": "/explicit/chrome",
            },
            clear=False,
        ):
            env = _browser_environment(self.layout)

        self.assertEqual(env["LOCAL_AGENT_PLAYWRIGHT_MODULE"], "/explicit/playwright")
        self.assertEqual(env["LOCAL_AGENT_CHROME_EXECUTABLE"], "/explicit/chrome")

    def test_login_reuses_existing_profile_without_manual_browser(self) -> None:
        session = FakeBrowserSession()

        result = login_live_slice_browser(
            self.layout,
            timeout_seconds=45,
            session_factory=lambda _layout: session,
            manual_login_factory=lambda _layout: self.fail(
                "manual browser must not open for an authenticated profile"
            ),
        )

        self.assertTrue(result["ok"])
        self.assertFalse(result["manual_login_used"])
        self.assertEqual(session.calls, ["wait_ready", "close"])
        self.assertEqual(session.wait_timeouts, [20])

    def test_login_uses_normal_browser_once_then_reuses_persisted_session(self) -> None:
        guest = FakeBrowserSession(
            ready={"ok": False, "reason": "chatgpt_login_timeout"}
        )
        authenticated = FakeBrowserSession()
        sessions = [guest, authenticated]
        manual_calls: list[Path] = []

        def manual_login(layout):
            manual_calls.append(layout.root)
            return {"ok": True, "reason": "manual_login_browser_closed"}

        result = login_live_slice_browser(
            self.layout,
            timeout_seconds=45,
            session_factory=lambda _layout: sessions.pop(0),
            manual_login_factory=manual_login,
        )

        self.assertTrue(result["ok"])
        self.assertTrue(result["manual_login_used"])
        self.assertEqual(manual_calls, [self.layout.root])
        self.assertEqual(guest.calls, ["wait_ready", "close"])
        self.assertEqual(authenticated.calls, ["wait_ready", "close"])
        self.assertEqual(guest.wait_timeouts, [20])
        self.assertEqual(authenticated.wait_timeouts, [30])

    def test_login_pause_consumes_arm_but_keeps_pending_attempt_rearmable(self) -> None:
        armed = self.prepare_and_arm()
        first = FakeBrowserSession(
            ready={"ok": False, "reason": "chatgpt_login_timeout"}
        )
        result = self.run_with(first, armed["arm"]["launch_nonce"])
        self.assertEqual(result["status"], "paused")
        self.assertEqual(self.spawns.load_attempt(REQUEST_ID, 1)["state"], "pending")
        self.assertEqual(load_runner_journal(self.layout)["phase"], "waiting_login")

        rearm = arm_live_slice(
            self.layout,
            expected_plan_digest=armed["prepared"]["plan_digest"],
            ttl_seconds=300,
            now=self.base_time + timedelta(seconds=1),
        )
        second = FakeBrowserSession()
        result = self.run_with(second, rearm["launch_nonce"])
        self.assertEqual(result["status"], "completed")
        self.assertIn("create", second.calls)
        self.assertNotIn("recover_create", second.calls)

    def test_uncertain_create_is_recovery_only_on_next_arm(self) -> None:
        armed = self.prepare_and_arm()
        first = FakeBrowserSession(
            create={"ok": False, "reason": "synthetic_create_uncertain"}
        )
        result = self.run_with(first, armed["arm"]["launch_nonce"])
        self.assertEqual(result["status"], "create_uncertain")
        self.assertEqual(self.spawns.load_attempt(REQUEST_ID, 1)["state"], "pending")
        self.assertEqual(load_runner_journal(self.layout)["phase"], "create_started")

        rearm = arm_live_slice(
            self.layout,
            expected_plan_digest=armed["prepared"]["plan_digest"],
            ttl_seconds=300,
            now=self.base_time + timedelta(seconds=1),
        )
        second = FakeBrowserSession(
            recover_create={"ok": True, "reason": "tab_recovered", "tabId": 77}
        )
        result = self.run_with(second, rearm["launch_nonce"])
        self.assertEqual(result["status"], "completed")
        self.assertIn("recover_create", second.calls)
        self.assertNotIn("create", second.calls)

    def test_missing_marker_after_uncertain_create_never_creates_replacement(self) -> None:
        armed = self.prepare_and_arm()
        first = FakeBrowserSession(
            create={"ok": False, "reason": "synthetic_create_uncertain"}
        )
        self.run_with(first, armed["arm"]["launch_nonce"])
        rearm = arm_live_slice(
            self.layout,
            expected_plan_digest=armed["prepared"]["plan_digest"],
            ttl_seconds=300,
            now=self.base_time + timedelta(seconds=1),
        )
        second = FakeBrowserSession(
            recover_create={"ok": False, "reason": "spawn_create_recovery_missing"}
        )

        result = self.run_with(second, rearm["launch_nonce"])

        self.assertEqual(result["status"], "manual_attach_required")
        self.assertEqual(self.spawns.load_attempt(REQUEST_ID, 1)["state"], "pending")
        self.assertEqual(second.calls.count("recover_create"), 1)
        self.assertNotIn("create", second.calls)
        self.assertNotIn("reattach_pre_submit", second.calls)

    def test_pre_submit_pause_resumes_same_tab_without_new_create(self) -> None:
        armed = self.prepare_and_arm()
        first = FakeBrowserSession(
            probe={"ok": False, "reason": "spawn_send_button_not_ready"}
        )
        result = self.run_with(first, armed["arm"]["launch_nonce"])
        self.assertEqual(result["status"], "paused")
        self.assertEqual(self.spawns.load_attempt(REQUEST_ID, 1)["state"], "bootstrap_ready")

        rearm = arm_live_slice(
            self.layout,
            expected_plan_digest=armed["prepared"]["plan_digest"],
            ttl_seconds=300,
            now=self.base_time + timedelta(seconds=1),
        )
        second = FakeBrowserSession()
        result = self.run_with(second, rearm["launch_nonce"])
        self.assertEqual(result["status"], "completed")
        self.assertNotIn("create", second.calls)
        self.assertNotIn("reattach_pre_submit", second.calls)
        self.assertIn("recover_create", second.calls)
        self.assertIn("probe", second.calls)
        self.assertIn("submit", second.calls)

    def test_pre_submit_missing_marker_reattaches_once_and_completes(self) -> None:
        armed = self.prepare_and_arm()
        first = FakeBrowserSession(
            probe={"ok": False, "reason": "spawn_send_button_not_ready"}
        )
        result = self.run_with(first, armed["arm"]["launch_nonce"])
        self.assertEqual(result["status"], "paused")

        rearm = arm_live_slice(
            self.layout,
            expected_plan_digest=armed["prepared"]["plan_digest"],
            ttl_seconds=300,
            now=self.base_time + timedelta(seconds=1),
        )
        second = FakeBrowserSession(
            recover_create={"ok": False, "reason": "spawn_create_recovery_missing"},
            reattach_pre_submit={"ok": True, "reason": "tab_reattached", "tabId": 77},
        )
        result = self.run_with(second, rearm["launch_nonce"])

        self.assertEqual(result["status"], "completed")
        self.assertEqual(second.calls.count("recover_create"), 1)
        self.assertEqual(second.calls.count("reattach_pre_submit"), 1)
        self.assertNotIn("create", second.calls)
        self.assertIn("probe", second.calls)
        self.assertIn("submit", second.calls)

    def test_pre_submit_reattach_is_never_repeated_after_second_pause(self) -> None:
        armed = self.prepare_and_arm()
        first = FakeBrowserSession(
            probe={"ok": False, "reason": "spawn_send_button_not_ready"}
        )
        self.run_with(first, armed["arm"]["launch_nonce"])

        rearm = arm_live_slice(
            self.layout,
            expected_plan_digest=armed["prepared"]["plan_digest"],
            ttl_seconds=300,
            now=self.base_time + timedelta(seconds=1),
        )
        second = FakeBrowserSession(
            recover_create={"ok": False, "reason": "spawn_create_recovery_missing"},
            reattach_pre_submit={"ok": True, "reason": "tab_reattached", "tabId": 77},
            probe={"ok": False, "reason": "spawn_send_button_not_ready"},
        )
        result = self.run_with(second, rearm["launch_nonce"])

        self.assertEqual(result["status"], "manual_attach_required")
        self.assertEqual(
            load_runner_journal(self.layout)["phase"],
            "waiting_pre_submit_after_reattach",
        )
        self.assertEqual(second.calls.count("reattach_pre_submit"), 1)

        rearm_again = arm_live_slice(
            self.layout,
            expected_plan_digest=armed["prepared"]["plan_digest"],
            ttl_seconds=300,
            now=self.base_time + timedelta(seconds=2),
        )
        third = FakeBrowserSession(
            recover_create={"ok": False, "reason": "spawn_create_recovery_missing"}
        )
        result = self.run_with(third, rearm_again["launch_nonce"])

        self.assertEqual(result["status"], "manual_attach_required")
        self.assertEqual(third.calls.count("recover_create"), 1)
        self.assertNotIn("reattach_pre_submit", third.calls)
        self.assertNotIn("create", third.calls)

    def test_manual_attach_registers_existing_ambiguous_child_without_retry(self) -> None:
        armed = self.prepare_and_arm()
        session = FakeBrowserSession(
            submit={
                "ok": False,
                "reason": "spawn_submission_ambiguous",
                "route": "child",
            }
        )
        result = self.run_with(session, armed["arm"]["launch_nonce"])
        self.assertEqual(result["status"], "manual_attach_required")
        self.assertEqual(self.spawns.load_attempt(REQUEST_ID, 1)["state"], "ambiguous")

        attached = attach_ambiguous_live_slice(
            self.layout,
            child_conversation_url=CHILD_URL,
            now=self.base_time + timedelta(seconds=2),
        )

        self.assertEqual(attached["status"], "completed")
        self.assertTrue(attached["recovered"])
        self.assertEqual(attached["resolution"], "manual_attach")
        self.assertEqual(attached["child_conversation_url"], CHILD_URL)
        self.assertEqual(self.spawns.load_attempt(REQUEST_ID, 1)["state"], "ambiguous")
        self.assertEqual(
            self.conversations.load_registration(REQUEST_ID)["child_conversation_url"],
            CHILD_URL,
        )
        self.assertEqual(self.conversations.load_state(REQUEST_ID)["state"], "active")
        self.assertNotIn(
            self.spawns.load_attempt(REQUEST_ID, 1)["id"],
            self.spawns.queue_snapshot()["unresolved_ambiguous_ids"],
        )
        self.assertFalse(live_slice_paths(self.layout).plan.exists())
        self.assertFalse(runner_paths(self.layout).journal.exists())
        self.assertTrue(runner_paths(self.layout).evidence.is_file())
        self.assertEqual(session.calls.count("submit"), 1)

    def test_registered_ambiguous_attach_crash_recovers_idempotently(self) -> None:
        armed = self.prepare_and_arm()
        session = FakeBrowserSession(
            submit={
                "ok": False,
                "reason": "spawn_submission_ambiguous",
                "route": "child",
            }
        )
        result = self.run_with(session, armed["arm"]["launch_nonce"])
        self.assertEqual(result["status"], "manual_attach_required")
        request = self.conversations.load_request(REQUEST_ID)
        self.conversations.register_child(
            {
                "schema_version": contract.CHILD_REGISTRATION_SCHEMA_VERSION,
                "child_request_id": REQUEST_ID,
                "child_request_digest": contract.child_request_digest(request),
                "parent_conversation_url": PARENT_URL,
                "child_conversation_url": CHILD_URL,
                "registered_at": "2026-09-24T18:33:00Z",
            }
        )
        self.assertEqual(self.conversations.load_state(REQUEST_ID)["state"], "active")
        self.assertEqual(self.spawns.load_attempt(REQUEST_ID, 1)["state"], "ambiguous")

        recovered = run_live_slice(
            self.layout,
            launch_nonce="already-consumed",
            session_factory=lambda _layout: self.fail("browser must not start"),
            now=lambda: self.base_time + timedelta(seconds=4),
        )

        self.assertEqual(recovered["status"], "completed")
        self.assertTrue(recovered["recovered"])
        self.assertEqual(recovered["resolution"], "manual_attach")
        self.assertEqual(recovered["spawn_state"], "ambiguous")
        self.assertEqual(recovered["child_state"], "active")
        self.assertFalse(runner_paths(self.layout).journal.exists())
        self.assertTrue(runner_paths(self.layout).evidence.is_file())

    def test_abandon_ambiguous_preserves_spawn_and_clears_runner_block(self) -> None:
        armed = self.prepare_and_arm()
        session = FakeBrowserSession(
            submit={
                "ok": False,
                "reason": "spawn_submission_ambiguous",
                "route": "child_identity_timeout",
            }
        )
        result = self.run_with(session, armed["arm"]["launch_nonce"])
        self.assertEqual(result["status"], "manual_attach_required")
        transaction = self.spawns.load_attempt(REQUEST_ID, 1)
        self.assertEqual(transaction["state"], "ambiguous")

        abandoned = abandon_ambiguous_live_slice(
            self.layout,
            now=self.base_time + timedelta(seconds=3),
        )

        self.assertEqual(abandoned["status"], "completed")
        self.assertTrue(abandoned["abandoned"])
        self.assertEqual(abandoned["resolution"], "abandoned_unresolved")
        self.assertEqual(abandoned["spawn_state"], "ambiguous")
        self.assertEqual(abandoned["child_state"], "abandoned")
        self.assertEqual(self.spawns.load_attempt(REQUEST_ID, 1)["state"], "ambiguous")
        self.assertEqual(self.conversations.load_state(REQUEST_ID)["state"], "abandoned")
        self.assertNotIn(
            transaction["id"],
            self.spawns.queue_snapshot()["unresolved_ambiguous_ids"],
        )
        self.assertFalse(live_slice_paths(self.layout).plan.exists())
        self.assertFalse(runner_paths(self.layout).journal.exists())
        self.assertTrue(runner_paths(self.layout).evidence.is_file())
        with self.assertRaisesRegex(ValueError, "requested or registration_pending"):
            self.spawns.enqueue(
                REQUEST_ID,
                created_at="2026-09-24T18:40:00Z",
            )
        self.assertEqual(len(self.spawns.load_attempts(REQUEST_ID)), 1)

    def test_failed_submit_becomes_ambiguous_and_never_auto_retries(self) -> None:
        armed = self.prepare_and_arm()
        session = FakeBrowserSession(
            submit={
                "ok": False,
                "reason": "spawn_submission_ambiguous",
                "route": "provisional_timeout",
                "diagnostic": {
                    "ok": True,
                    "reason": "spawn_provisional_diagnostic",
                    "claimState": "submitted",
                    "exactUserMessage": True,
                    "assistantGenerating": True,
                },
            }
        )

        result = self.run_with(session, armed["arm"]["launch_nonce"])

        self.assertEqual(result["status"], "manual_attach_required")
        self.assertEqual(result["browser_route"], "provisional_timeout")
        self.assertEqual(result["browser_diagnostic"]["claimState"], "submitted")
        self.assertTrue(result["browser_diagnostic"]["exactUserMessage"])
        self.assertTrue(result["browser_diagnostic"]["assistantGenerating"])
        transaction = self.spawns.load_attempt(REQUEST_ID, 1)
        self.assertEqual(transaction["state"], "ambiguous")
        self.assertFalse(result["needs_rearm"])
        with self.assertRaisesRegex(RuntimeError, "terminal or ambiguous"):
            arm_live_slice(
                self.layout,
                expected_plan_digest=armed["prepared"]["plan_digest"],
                now=self.base_time + timedelta(seconds=1),
            )

    def test_bootstrap_submitting_restart_reconciles_without_second_submit(self) -> None:
        armed = self.prepare_and_arm()
        transaction = self.spawns.begin_browser_attempt(
            REQUEST_ID,
            1,
            tab_id=91,
            updated_at="2026-09-24T18:20:00Z",
        )
        self.assertEqual(transaction["state"], "tab_created")
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
        session = FakeBrowserSession()

        result = self.run_with(session, armed["arm"]["launch_nonce"])

        self.assertEqual(result["status"], "completed")
        self.assertIn("reconcile", session.calls)
        self.assertNotIn("submit", session.calls)
        self.assertNotIn("create", session.calls)

    def test_registration_written_before_crash_is_completed_without_new_arm(self) -> None:
        armed = self.prepare_and_arm()
        self.spawns.begin_browser_attempt(
            REQUEST_ID,
            1,
            tab_id=101,
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
        live_slice_paths(self.layout).arm.unlink()

        result = run_live_slice(
            self.layout,
            launch_nonce="already-consumed",
            session_factory=lambda _layout: self.fail("browser must not start"),
            now=self.now,
        )

        self.assertEqual(result["status"], "completed")
        self.assertTrue(result["recovered"])
        self.assertEqual(self.spawns.load_attempt(REQUEST_ID, 1)["state"], "done")
        self.assertTrue(runner_paths(self.layout).evidence.is_file())


if __name__ == "__main__":
    unittest.main()
