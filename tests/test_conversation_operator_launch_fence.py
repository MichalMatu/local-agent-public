"""Fail-closed launch recovery for optional Conversation Fabric operator campaigns."""

from __future__ import annotations

import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import local_agent.daemon.service as agentd
from local_agent.conversation import operator_queue
from local_agent.supervisor import conversation, observability


PARENT = "https://chatgpt.com/c/6abfcdf9-8438-83eb-86da-1c4b209afc47"


def request_payload(request_id: str = "operator-request-001") -> dict:
    return {
        "schema_version": 1,
        "id": request_id,
        "workflow_id": "operator-workflow-001",
        "parent_conversation_url": PARENT,
        "children": [
            {
                "request_id": "child-001",
                "node_id": "node-001",
                "role": "verification",
                "summary": "Check an isolated launch fence.",
                "paths": ["local_agent/conversation/operator_queue.py"],
            }
        ],
    }


def environment(root: Path) -> dict[str, str]:
    return {
        conversation.OPERATOR_ENABLED_ENV: "1",
        conversation.OPERATOR_HOME_ENV: str(root / "home"),
        conversation.OPERATOR_ROOT_ENV: str(root / "lab"),
        conversation.OPERATOR_CHECKOUT_ENV: str(root / "checkout"),
        conversation.OPERATOR_PRODUCTION_CHECKOUT_ENV: str(root / "production"),
    }


class LaunchFenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.state = self.root / "state"
        self.control = self.root / "control"
        self.control_requests = self.control / operator_queue.CONTROL_REQUEST_DIR
        self.control_requests.mkdir(parents=True)
        self.payload = request_payload()
        self.write_control_request(self.payload)
        self.item = operator_queue.stage_next_control_request(self.control, self.state)
        assert self.item is not None

    def write_control_request(self, request: dict) -> None:
        (self.control_requests / f"{request['id']}.json").write_text(
            json.dumps(request), encoding="utf-8"
        )

    def test_first_launch_is_durable_and_second_launch_is_rejected(self) -> None:
        self.assertFalse(operator_queue.launch_reconciliation_required(self.state, self.item))
        self.assertTrue(operator_queue.reserve_launch_once(self.state, self.item))
        self.assertTrue(operator_queue.launch_reconciliation_required(self.state, self.item))
        fence = operator_queue._launch_fence_path(self.state, self.item.request_id)
        self.assertEqual(
            json.loads(fence.read_text(encoding="utf-8")),
            {
                "schema_version": 1,
                "request_id": self.item.request_id,
                "request_digest": self.item.request_digest,
            },
        )
        self.assertFalse(operator_queue.reserve_launch_once(self.state, self.item))
        self.assertIsNotNone(operator_queue.next_staged_request(self.state))

    def test_new_launch_directory_entry_is_durable_before_reservation(self) -> None:
        fence = operator_queue._launch_fence_path(self.state, self.item.request_id)
        self.assertFalse(fence.parent.exists())
        with mock.patch.object(
            operator_queue, "fsync_directory", wraps=operator_queue.fsync_directory
        ) as synced:
            self.assertTrue(operator_queue.reserve_launch_once(self.state, self.item))
        self.assertEqual(
            synced.call_args_list,
            [
                mock.call(fence.parent.parent.parent),
                mock.call(fence.parent.parent),
                mock.call(fence.parent),
            ],
        )
        self.assertTrue(fence.is_file())

    def test_competing_starts_admit_exactly_one(self) -> None:
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(
                lambda _: operator_queue.reserve_launch_once(self.state, self.item),
                range(2),
            ))
        self.assertCountEqual(results, [True, False])

    def test_corrupt_or_symlink_fence_cannot_enable_replay(self) -> None:
        fence = operator_queue._launch_fence_path(self.state, self.item.request_id)
        fence.parent.mkdir(parents=True)
        fence.write_text("{", encoding="utf-8")
        self.assertFalse(operator_queue.reserve_launch_once(self.state, self.item))
        fence.unlink()
        victim = self.root / "unrelated"
        victim.write_text("safe", encoding="utf-8")
        fence.symlink_to(victim)
        self.assertFalse(operator_queue.reserve_launch_once(self.state, self.item))
        self.assertEqual(victim.read_text(encoding="utf-8"), "safe")

    def test_spawn_restart_and_interruption_do_not_relaunch(self) -> None:
        proc = SimpleNamespace(pid=1234, poll=lambda: 143)
        with mock.patch.dict(conversation.os.environ, environment(self.root), clear=True), \
             mock.patch.object(agentd, "STATE_DIR", self.state), \
             mock.patch.object(conversation, "repository_root", return_value=self.root), \
             mock.patch.object(conversation, "popen_registered", return_value=proc) as spawn, \
             mock.patch.object(conversation, "unregister_process"):
            slot = conversation.start_if_pending()
            self.assertIsNotNone(slot)
            assert slot is not None
            self.assertIsNone(conversation.reap(slot, interrupted=True))
            self.assertIsNone(conversation.start_if_pending())
            spawn.assert_called_once()
            status = observability.operator_observability(None)
            self.assertTrue(status["launch_reconciliation_required"])
        self.assertTrue(self.item.request_path.exists())
        self.assertFalse(self.item.result_path.exists())

    def test_spawn_exception_is_ambiguous_and_cannot_replay(self) -> None:
        with mock.patch.dict(conversation.os.environ, environment(self.root), clear=True), \
             mock.patch.object(agentd, "STATE_DIR", self.state), \
             mock.patch.object(conversation, "repository_root", return_value=self.root), \
             mock.patch.object(conversation, "popen_registered", side_effect=OSError("spawn")) as spawn:
            with self.assertRaisesRegex(OSError, "spawn"):
                conversation.start_if_pending()
            self.assertIsNone(conversation.start_if_pending())
            spawn.assert_called_once()

    def test_publication_cleanup_clears_fence_but_not_before(self) -> None:
        self.assertTrue(operator_queue.reserve_launch_once(self.state, self.item))
        self.assertTrue(operator_queue.launch_reconciliation_required(self.state, self.item))
        # Production calls discard only after fresh, matching origin publication.
        operator_queue.discard_spool(self.item)
        self.assertFalse(operator_queue.launch_reconciliation_required(self.state, self.item))
        self.assertFalse(self.item.request_path.exists())

    def test_launch_fence_is_request_scoped(self) -> None:
        self.assertTrue(operator_queue.reserve_launch_once(self.state, self.item))
        other = request_payload("operator-request-002")
        self.write_control_request(other)
        other_item = operator_queue.OperatorWorkItem(
            request_id=other["id"],
            request_digest="sha256:" + "a" * 64,
            request_path=self.item.request_path.parent / "operator-request-002.json",
            result_path=self.item.result_path.parent / "operator-request-002.json",
        )
        self.assertTrue(operator_queue.reserve_launch_once(self.state, other_item))
        self.assertFalse(operator_queue.reserve_launch_once(self.state, other_item))


if __name__ == "__main__":
    unittest.main()
