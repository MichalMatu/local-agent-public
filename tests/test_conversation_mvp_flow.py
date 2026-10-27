from __future__ import annotations

import copy
import tempfile
import unittest
from unittest import mock
from datetime import datetime, timezone
from pathlib import Path

from local_agent.conversation import contract
from local_agent.conversation.store import WorkflowConversationStore
from local_agent.development.lab import build_dev_lab_layout, initialize_dev_lab
from local_agent.development.live_seed import CheckoutIdentity
from local_agent.development.mvp_flow import RESULT_STABLE_OBSERVATIONS, run_mvp_campaign
from local_agent.workflow.store import WorkflowStore

BINDING = "12345678-1234-4abc-8def-1234567890ab"
PARENT = "https://chatgpt.com/c/11111111-1111-4111-8111-111111111111"
CHILDREN = {
    "mvp-child-a": "https://chatgpt.com/c/22222222-2222-4222-8222-222222222222",
    "mvp-child-b": "https://chatgpt.com/c/33333333-3333-4333-8333-333333333333",
}


class FakeResultSession:
    def __init__(self, layout):
        self.layout = layout
        self.opened = []
        self.closed = []
        self.observations = {}

    def wait_ready(self, *, timeout_seconds):
        return {"ok": True, "reason": "chatgpt_ready"}

    def open_child(self, child_url, ownership=None):
        self.opened.append(child_url)
        return {"ok": True, "reason": "child_opened", "child_conversation_url": child_url}

    def observe_child(self, child_url, ownership=None):
        count = self.observations.get(child_url, 0) + 1
        self.observations[child_url] = count
        request_id = next(key for key, value in CHILDREN.items() if value == child_url)
        return {
            "ok": True,
            "reason": "child_result_ready",
            "child_conversation_url": child_url,
            "assistant_identity": f"assistant-{request_id}",
            "assistant_text": f"Result for {request_id}.",
            "truncated": False,
        }

    def close_child(self, child_url, ownership=None):
        workflow = WorkflowStore(self.layout.state_dir)
        conversations = WorkflowConversationStore(workflow, "mvp-workflow")
        request_id = next(key for key, value in CHILDREN.items() if value == child_url)
        if conversations.load_state(request_id)["state"] != "retired":
            raise AssertionError("child closed before durable retirement")
        self.closed.append(child_url)
        return {"ok": True, "reason": "child_closed", "child_conversation_url": child_url}

    def close(self):
        return None


class ConversationMvpFlowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.layout = build_dev_lab_layout(home=self.home)
        extension = self.layout.checkout / "chat_bridge"
        extension.mkdir(parents=True)
        (extension / "manifest.json").write_text("{}\n", encoding="utf-8")
        initialize_dev_lab(self.layout)
        self.identity = CheckoutIdentity(
            repository_id="local-agent",
            repository="example-owner/local-agent",
            agent_binding=BINDING,
            repository_ref="work/conversation-fabric-mvp-e2e-20261002",
            repository_commit_sha="a" * 40,
        )
        self.spec = {
            "schema_version": 1,
            "workflow_id": "mvp-workflow",
            "parent_conversation_url": PARENT,
            "children": [
                {
                    "request_id": "mvp-child-a",
                    "node_id": "mvp-node-a",
                    "role": "verification",
                    "summary": "Return one bounded verification result.",
                    "paths": ["local_agent/development"],
                },
                {
                    "request_id": "mvp-child-b",
                    "node_id": "mvp-node-b",
                    "role": "research",
                    "summary": "Return one bounded research result.",
                    "paths": ["local_agent/conversation"],
                },
            ],
        }
        self.base_time = datetime(2026, 10, 2, 20, 0, tzinfo=timezone.utc)

    def identity_provider(self, _layout):
        return self.identity

    def _seed_for_test(self, spec):
        from local_agent.development.mvp_flow import _seed_campaign
        return _seed_campaign(
            self.layout,
            spec,
            now=self.base_time,
            identity_provider=self.identity_provider,
        )

    def fake_spawn(self, layout, workflow_id, request_id, *, login_timeout_seconds):
        workflow = WorkflowStore(layout.state_dir)
        conversations = WorkflowConversationStore(workflow, workflow_id)
        request = conversations.load_request(request_id)
        state = conversations.load_state(request_id)["state"]
        if state == "requested":
            conversations.transition_state(request_id, "registration_pending")
        conversations.register_child({
            "schema_version": contract.CHILD_REGISTRATION_SCHEMA_VERSION,
            "child_request_id": request_id,
            "child_request_digest": contract.child_request_digest(request),
            "parent_conversation_url": request["parent_conversation_url"],
            "child_conversation_url": CHILDREN[request_id],
            "registered_at": "2026-10-02T20:01:00Z",
        })
        return {"status": "completed", "child_conversation_url": CHILDREN[request_id]}

    def test_batch_flow_records_results_retires_then_closes(self):
        holder = {}

        def factory(layout):
            session = FakeResultSession(layout)
            holder["session"] = session
            return session

        result = run_mvp_campaign(
            self.layout,
            self.spec,
            login_timeout_seconds=30,
            result_timeout_seconds=30,
            poll_seconds=0,
            identity_provider=self.identity_provider,
            spawn_runner=self.fake_spawn,
            result_session_factory=factory,
            now=lambda: self.base_time,
        )

        self.assertEqual(result["status"], "completed")
        self.assertEqual(len(result["children"]), 2)
        workflow = WorkflowStore(self.layout.state_dir)
        conversations = WorkflowConversationStore(workflow, "mvp-workflow")
        for request_id, child_url in CHILDREN.items():
            self.assertEqual(conversations.load_state(request_id)["state"], "retired")
            self.assertIsNotNone(conversations.load_terminal_record(request_id))
            self.assertIsNotNone(conversations.load_adoption_record(request_id))
            evidence = (
                self.layout.state_dir
                / "conversation-mvp"
                / "mvp-workflow"
                / "results"
                / f"{request_id}.json"
            )
            self.assertTrue(evidence.is_file())
            self.assertIn(child_url, holder["session"].closed)
            self.assertGreaterEqual(
                holder["session"].observations[child_url], RESULT_STABLE_OBSERVATIONS
            )
        state = workflow.load_state("mvp-workflow")
        self.assertEqual(set(state["node_states"].values()), {"succeeded"})

    def test_abandoned_child_does_not_block_later_child_spawn(self):
        workflow, conversations, requests = self._seed_for_test(self.spec)
        conversations.transition_state("mvp-child-a", "registration_pending")
        conversations.transition_state("mvp-child-a", "abandoned")
        spawned = []

        def spawn_later(layout, workflow_id, request_id, *, login_timeout_seconds):
            spawned.append(request_id)
            return self.fake_spawn(
                layout,
                workflow_id,
                request_id,
                login_timeout_seconds=login_timeout_seconds,
            )

        result = run_mvp_campaign(
            self.layout,
            self.spec,
            login_timeout_seconds=30,
            result_timeout_seconds=30,
            poll_seconds=0,
            identity_provider=self.identity_provider,
            spawn_runner=spawn_later,
            result_session_factory=FakeResultSession,
            now=lambda: self.base_time,
        )

        self.assertEqual(result["status"], "incomplete")
        self.assertNotIn("mvp-child-a", spawned)
        self.assertIn("mvp-child-b", spawned)
        self.assertEqual(conversations.load_state("mvp-child-a")["state"], "abandoned")
        self.assertEqual(conversations.load_state("mvp-child-b")["state"], "retired")

    def test_observe_transport_failure_restarts_session_once(self):
        spec = copy.deepcopy(self.spec)
        spec["children"] = spec["children"][:1]
        sessions = []

        class FailingObserveSession(FakeResultSession):
            def observe_child(self, child_url, ownership=None):
                raise RuntimeError("synthetic observe transport timeout")

        def factory(layout):
            session = FailingObserveSession(layout) if not sessions else FakeResultSession(layout)
            sessions.append(session)
            return session

        result = run_mvp_campaign(
            self.layout,
            spec,
            login_timeout_seconds=30,
            result_timeout_seconds=30,
            poll_seconds=0,
            identity_provider=self.identity_provider,
            spawn_runner=self.fake_spawn,
            result_session_factory=factory,
            now=lambda: self.base_time,
        )

        self.assertEqual(result["status"], "completed")
        self.assertEqual(len(sessions), 2)
        conversations = WorkflowConversationStore(
            WorkflowStore(self.layout.state_dir), "mvp-workflow"
        )
        self.assertEqual(conversations.load_state("mvp-child-a")["state"], "retired")

    def test_spawn_failure_still_finalizes_already_registered_child(self):
        spec = copy.deepcopy(self.spec)
        holder = {}

        def partial_spawn(layout, workflow_id, request_id, *, login_timeout_seconds):
            if request_id == "mvp-child-b":
                raise RuntimeError("synthetic ambiguous spawn")
            return self.fake_spawn(
                layout,
                workflow_id,
                request_id,
                login_timeout_seconds=login_timeout_seconds,
            )

        def factory(layout):
            session = FakeResultSession(layout)
            holder["session"] = session
            return session

        result = run_mvp_campaign(
            self.layout,
            spec,
            login_timeout_seconds=30,
            result_timeout_seconds=30,
            poll_seconds=0,
            identity_provider=self.identity_provider,
            spawn_runner=partial_spawn,
            result_session_factory=factory,
            now=lambda: self.base_time,
        )

        self.assertEqual(result["status"], "incomplete")
        self.assertIn("mvp-child-b", result["failures"])
        conversations = WorkflowConversationStore(
            WorkflowStore(self.layout.state_dir), "mvp-workflow"
        )
        self.assertEqual(conversations.load_state("mvp-child-a")["state"], "retired")
        self.assertIn(CHILDREN["mvp-child-a"], holder["session"].closed)
        self.assertNotEqual(conversations.load_state("mvp-child-b")["state"], "retired")

    def test_terminal_recorded_recovery_reuses_existing_result_evidence(self):
        spec = copy.deepcopy(self.spec)
        spec["children"] = spec["children"][:1]
        original = WorkflowConversationStore.record_adoption
        failed = {"value": False}

        def fail_once(store, record):
            if not failed["value"]:
                failed["value"] = True
                raise OSError("simulated crash after terminal record")
            return original(store, record)

        with mock.patch.object(WorkflowConversationStore, "record_adoption", new=fail_once):
            first = run_mvp_campaign(
                self.layout,
                spec,
                login_timeout_seconds=30,
                result_timeout_seconds=30,
                poll_seconds=0,
                identity_provider=self.identity_provider,
                spawn_runner=self.fake_spawn,
                result_session_factory=FakeResultSession,
                now=lambda: self.base_time,
            )
        self.assertEqual(first["status"], "incomplete")
        conversations = WorkflowConversationStore(
            WorkflowStore(self.layout.state_dir), "mvp-workflow"
        )
        self.assertEqual(
            conversations.load_state("mvp-child-a")["state"], "terminal_recorded"
        )
        evidence = (
            self.layout.state_dir
            / "conversation-mvp"
            / "mvp-workflow"
            / "results"
            / "mvp-child-a.json"
        )
        before = evidence.read_bytes()

        second = run_mvp_campaign(
            self.layout,
            spec,
            login_timeout_seconds=30,
            result_timeout_seconds=30,
            poll_seconds=0,
            identity_provider=self.identity_provider,
            spawn_runner=self.fake_spawn,
            result_session_factory=FakeResultSession,
            now=lambda: self.base_time,
        )
        self.assertEqual(second["status"], "completed")
        self.assertEqual(evidence.read_bytes(), before)
        self.assertEqual(conversations.load_state("mvp-child-a")["state"], "retired")

    def test_retired_close_failure_is_retried_on_next_run(self):
        spec = copy.deepcopy(self.spec)
        spec["children"] = spec["children"][:1]

        class FailingCloseSession(FakeResultSession):
            def close_child(self, child_url, ownership=None):
                workflow = WorkflowStore(self.layout.state_dir)
                conversations = WorkflowConversationStore(workflow, "mvp-workflow")
                request_id = next(key for key, value in CHILDREN.items() if value == child_url)
                if conversations.load_state(request_id)["state"] != "retired":
                    raise AssertionError("child closed before durable retirement")
                return {"ok": False, "reason": "synthetic_close_failure"}

        first = run_mvp_campaign(
            self.layout,
            spec,
            login_timeout_seconds=30,
            result_timeout_seconds=30,
            poll_seconds=0,
            identity_provider=self.identity_provider,
            spawn_runner=self.fake_spawn,
            result_session_factory=FailingCloseSession,
            now=lambda: self.base_time,
        )
        self.assertEqual(first["status"], "incomplete")
        self.assertIn("mvp-child-a", first["failures"])
        conversations = WorkflowConversationStore(
            WorkflowStore(self.layout.state_dir), "mvp-workflow"
        )
        self.assertEqual(conversations.load_state("mvp-child-a")["state"], "retired")

        holder = {}

        def factory(layout):
            session = FakeResultSession(layout)
            holder["session"] = session
            return session

        second = run_mvp_campaign(
            self.layout,
            spec,
            login_timeout_seconds=30,
            result_timeout_seconds=30,
            poll_seconds=0,
            identity_provider=self.identity_provider,
            spawn_runner=self.fake_spawn,
            result_session_factory=factory,
            now=lambda: self.base_time,
        )
        self.assertEqual(second["status"], "completed")
        self.assertIn(CHILDREN["mvp-child-a"], holder["session"].closed)


if __name__ == "__main__":
    unittest.main()
