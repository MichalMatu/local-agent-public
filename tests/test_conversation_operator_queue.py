from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from local_agent.conversation import operator_queue
from local_agent.conversation.operator_contract import build_operator_result


PARENT_URL = "https://chatgpt.com/c/6abfcdf9-8438-83eb-86da-1c4b209afc47"
CHILD_URL = "https://chatgpt.com/c/6ac0456e-f1b0-83eb-ad85-5de71ce63530"
DIGEST = "sha256:" + ("a" * 64)


def request_payload(request_id: str = "operator-request-001") -> dict:
    return {
        "schema_version": 1,
        "id": request_id,
        "workflow_id": "operator-workflow-001",
        "parent_conversation_url": PARENT_URL,
        "children": [
            {
                "request_id": "operator-child-001",
                "node_id": "operator-node-001",
                "role": "verification",
                "summary": "Verify the operator queue.",
                "paths": ["local_agent/conversation/operator_queue.py"],
            }
        ],
    }


def completed_result(request: dict) -> dict:
    return build_operator_result(
        request,
        {
            "children": [
                {
                    "request_id": "operator-child-001",
                    "child_state": "retired",
                    "summary": "QUEUE_OK",
                    "child_conversation_url": CHILD_URL,
                    "evidence_digest": DIGEST,
                }
            ]
        },
    )


class OperatorQueueTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.control = root / "control"
        self.state = root / "state"
        self.control.mkdir()
        request_dir = self.control / operator_queue.CONTROL_REQUEST_DIR
        request_dir.mkdir(parents=True)
        self.request = request_payload()
        (request_dir / f"{self.request['id']}.json").write_text(
            json.dumps(self.request), encoding="utf-8"
        )

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_stage_is_single_flight_and_idempotent(self) -> None:
        first = operator_queue.stage_next_control_request(self.control, self.state)
        self.assertIsNotNone(first)
        assert first is not None
        second = operator_queue.stage_next_control_request(self.control, self.state)
        self.assertEqual(first.request_id, second.request_id if second else None)
        self.assertEqual(first.request_path.read_text(encoding="utf-8").strip()[0], "{")
        self.assertFalse(first.result_path.exists())

    def test_restart_discards_local_spool_when_exact_remote_result_is_already_published(self) -> None:
        item = operator_queue.stage_next_control_request(self.control, self.state)
        assert item is not None
        result = completed_result(self.request)
        operator_queue.persist_spooled_result(item.result_path, self.request, result)
        result_dir = self.control / operator_queue.CONTROL_RESULT_DIR
        result_dir.mkdir(parents=True)
        (result_dir / f"{self.request['id']}.json").write_text(
            json.dumps(result), encoding="utf-8"
        )

        self.assertIsNone(operator_queue.stage_next_control_request(self.control, self.state))
        self.assertFalse(item.request_path.exists())
        self.assertFalse(item.result_path.exists())
        self.assertIsNone(operator_queue.next_staged_request(self.state))

    def test_control_result_skips_already_completed_request(self) -> None:
        result_dir = self.control / operator_queue.CONTROL_RESULT_DIR
        result_dir.mkdir(parents=True)
        result = completed_result(self.request)
        (result_dir / f"{self.request['id']}.json").write_text(
            json.dumps(result), encoding="utf-8"
        )
        self.assertIsNone(operator_queue.stage_next_control_request(self.control, self.state))

    def test_worker_failure_becomes_bounded_spooled_result(self) -> None:
        item = operator_queue.stage_next_control_request(self.control, self.state)
        assert item is not None
        result = operator_queue.persist_worker_failure(item, "synthetic worker exit")
        self.assertEqual(result["state"], "failed")
        self.assertEqual(result["children_failed"], 1)
        ready = operator_queue.next_spooled_result(self.state)
        self.assertIsNotNone(ready)
        assert ready is not None
        ready_item, request, loaded = ready
        self.assertEqual(ready_item.request_id, self.request["id"])
        self.assertEqual(request, self.request)
        self.assertEqual(loaded, result)

    def test_published_result_conflict_fails_closed(self) -> None:
        result_dir = self.control / operator_queue.CONTROL_RESULT_DIR
        result_dir.mkdir(parents=True)
        result = completed_result(self.request)
        conflicting = dict(result)
        conflicting["state"] = "failed"
        (result_dir / f"{self.request['id']}.json").write_text(
            json.dumps(conflicting), encoding="utf-8"
        )
        with self.assertRaises(ValueError):
            operator_queue.control_result_matches(self.control, self.request, result)

    def test_remote_request_mutation_after_stage_fails_closed(self) -> None:
        item = operator_queue.stage_next_control_request(self.control, self.state)
        assert item is not None
        mutated = request_payload()
        mutated["children"][0]["summary"] = "mutated after staging"
        request_path = (
            self.control
            / operator_queue.CONTROL_REQUEST_DIR
            / f"{self.request['id']}.json"
        )
        request_path.write_text(json.dumps(mutated), encoding="utf-8")
        with self.assertRaisesRegex(RuntimeError, "changed after local staging"):
            operator_queue.require_control_request_match(self.control, self.request)

    def test_remote_request_removal_after_stage_fails_closed(self) -> None:
        item = operator_queue.stage_next_control_request(self.control, self.state)
        assert item is not None
        request_path = (
            self.control
            / operator_queue.CONTROL_REQUEST_DIR
            / f"{self.request['id']}.json"
        )
        request_path.unlink()
        with self.assertRaisesRegex(RuntimeError, "disappeared after local staging"):
            operator_queue.require_control_request_match(self.control, self.request)

    def test_discard_spool_removes_request_and_result(self) -> None:
        item = operator_queue.stage_next_control_request(self.control, self.state)
        assert item is not None
        operator_queue.persist_spooled_result(
            item.result_path,
            self.request,
            completed_result(self.request),
        )
        operator_queue.discard_spool(item)
        self.assertFalse(item.request_path.exists())
        self.assertFalse(item.result_path.exists())


if __name__ == "__main__":
    unittest.main()
