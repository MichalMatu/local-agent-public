from __future__ import annotations

import copy
import tempfile
import unittest
from pathlib import Path

from local_agent.conversation.operator_contract import (
    MAX_OPERATOR_ERROR_CHARS,
    MAX_OPERATOR_SUMMARY_CHARS,
    build_operator_result,
    load_operator_request,
    operator_request_digest,
    operator_request_repository_id,
    operator_request_repository_ids,
    request_to_mvp_spec,
    validate_operator_request,
    validate_operator_result,
)


PARENT_URL = "https://chatgpt.com/c/6abfcdf9-8438-83eb-86da-1c4b209afc47"
CHILD_URL = "https://chatgpt.com/c/6ac04019-0698-83eb-8bcd-708b01bd08d8"
DIGEST = "sha256:" + ("a" * 64)


def sample_request() -> dict:
    return {
        "schema_version": 1,
        "id": "operator-request-001",
        "workflow_id": "operator-workflow-001",
        "parent_conversation_url": PARENT_URL,
        "children": [
            {
                "request_id": "operator-child-001",
                "node_id": "operator-node-001",
                "role": "research",
                "summary": "Audit the operator entrypoint.",
                "paths": ["local_agent/development/mvp_flow.py"],
            },
            {
                "request_id": "operator-child-002",
                "node_id": "operator-node-002",
                "role": "verification",
                "summary": "Verify the bounded result view.",
                "paths": ["local_agent/conversation/operator_contract.py"],
            },
        ],
    }


def targeted_request() -> dict:
    request = sample_request()
    request["schema_version"] = 2
    request["repository_id"] = "example-project"
    return request


def multirepo_request() -> dict:
    request = sample_request()
    request["schema_version"] = 3
    request["repository_ids"] = ["example-device", "example-project"]
    return request


class OperatorContractTests(unittest.TestCase):
    def test_request_digest_is_stable_and_v1_mapping_is_unchanged(self) -> None:
        request = sample_request()
        validate_operator_request(request)
        first = operator_request_digest(request)
        second = operator_request_digest(copy.deepcopy(request))
        self.assertEqual(first, second)
        self.assertRegex(first, r"^sha256:[0-9a-f]{64}$")
        self.assertEqual(
            request_to_mvp_spec(request),
            {
                "schema_version": 1,
                "workflow_id": request["workflow_id"],
                "parent_conversation_url": request["parent_conversation_url"],
                "children": request["children"],
            },
        )
        self.assertIsNone(operator_request_repository_id(request))
        self.assertEqual(operator_request_repository_ids(request), [])

    def test_v2_repository_id_is_reasoning_context_not_execution_identity(self) -> None:
        request = targeted_request()
        validate_operator_request(request)
        digest = operator_request_digest(request)
        self.assertEqual(operator_request_repository_id(request), "example-project")
        self.assertEqual(operator_request_repository_ids(request), ["example-project"])
        spec = request_to_mvp_spec(request)
        self.assertNotIn("repository_id", spec)
        self.assertTrue(
            spec["children"][0]["summary"].startswith(
                "Reasoning repository context (not execution authority): example-project. "
            )
        )

        changed = copy.deepcopy(request)
        changed["repository_id"] = "example-device"
        self.assertNotEqual(operator_request_digest(changed), digest)

    def test_v3_multirepo_context_preserves_order_without_selecting_a_binding(self) -> None:
        request = multirepo_request()
        validate_operator_request(request)
        self.assertEqual(operator_request_repository_ids(request), ["example-device", "example-project"])
        self.assertIsNone(operator_request_repository_id(request))
        spec = request_to_mvp_spec(request)
        self.assertTrue(
            spec["children"][0]["summary"].startswith(
                "Reasoning repository context (not execution authority): example-device, example-project. "
            )
        )
        self.assertNotIn("repository_ids", spec)

        duplicate = multirepo_request()
        duplicate["repository_ids"] = ["example-project", "EXAMPLE-PROJECT"]
        with self.assertRaisesRegex(ValueError, "duplicate operator repository_id"):
            validate_operator_request(duplicate)

    def test_v2_and_v3_require_canonical_repository_context(self) -> None:
        missing = targeted_request()
        missing.pop("repository_id")
        with self.assertRaisesRegex(ValueError, "repository_id"):
            validate_operator_request(missing)

        invalid = targeted_request()
        invalid["repository_id"] = "Grow Clip"
        with self.assertRaisesRegex(ValueError, "repository_id"):
            validate_operator_request(invalid)

        empty = multirepo_request()
        empty["repository_ids"] = []
        with self.assertRaisesRegex(ValueError, "repository_ids"):
            validate_operator_request(empty)

    def test_request_rejects_duplicate_child_identity_and_unknown_role(self) -> None:
        duplicate = sample_request()
        duplicate["children"][1]["request_id"] = duplicate["children"][0]["request_id"]
        with self.assertRaisesRegex(ValueError, "must be unique"):
            validate_operator_request(duplicate)

        unknown = sample_request()
        unknown["children"][0]["role"] = "review"
        with self.assertRaisesRegex(ValueError, "operator child role"):
            validate_operator_request(unknown)

    def test_load_request_rejects_symlink(self) -> None:
        request = sample_request()
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            target = root / "request.json"
            target.write_text(__import__("json").dumps(request), encoding="utf-8")
            link = root / "link.json"
            link.symlink_to(target)
            with self.assertRaisesRegex(ValueError, "unsafe"):
                load_operator_request(link)

    def test_completed_campaign_projects_bounded_operator_view(self) -> None:
        request = sample_request()
        campaign = {
            "children": [
                {
                    "request_id": "operator-child-001",
                    "child_state": "retired",
                    "summary": "A" * (MAX_OPERATOR_SUMMARY_CHARS + 50),
                    "child_conversation_url": CHILD_URL,
                    "evidence_digest": DIGEST,
                },
                {
                    "request_id": "operator-child-002",
                    "child_state": "retired",
                    "summary": "verified",
                    "child_conversation_url": CHILD_URL,
                    "evidence_digest": DIGEST,
                },
            ]
        }
        result = build_operator_result(request, campaign)
        validate_operator_result(result, request)
        self.assertEqual(result["state"], "completed")
        self.assertEqual(result["children_completed"], 2)
        self.assertEqual(len(result["children"][0]["summary"]), MAX_OPERATOR_SUMMARY_CHARS)
        self.assertIsNone(result["children"][0]["error"])

    def test_waiting_and_failed_children_are_normalized_and_error_bounded(self) -> None:
        request = sample_request()
        campaign = {
            "children": [
                {
                    "request_id": "operator-child-001",
                    "child_state": "registration_pending",
                    "error": "X" * (MAX_OPERATOR_ERROR_CHARS + 100),
                    "child_conversation_url": None,
                },
                {
                    "request_id": "operator-child-002",
                    "child_state": "abandoned",
                    "error": "identity unresolved",
                    "child_conversation_url": None,
                },
            ]
        }
        result = build_operator_result(request, campaign)
        self.assertEqual(result["state"], "failed")
        self.assertEqual(result["children_waiting"], 0)
        self.assertEqual(result["children_failed"], 2)
        self.assertEqual(len(result["children"][0]["error"]), MAX_OPERATOR_ERROR_CHARS)
        self.assertIsNone(result["children"][0]["summary"])

    def test_result_validation_detects_request_digest_and_count_conflicts(self) -> None:
        request = sample_request()
        result = build_operator_result(
            request,
            {
                "children": [
                    {
                        "request_id": "operator-child-001",
                        "child_state": "retired",
                        "summary": "one",
                        "child_conversation_url": CHILD_URL,
                        "evidence_digest": DIGEST,
                    },
                    {
                        "request_id": "operator-child-002",
                        "child_state": "retired",
                        "summary": "two",
                        "child_conversation_url": CHILD_URL,
                        "evidence_digest": DIGEST,
                    },
                ]
            },
        )
        bad_digest = copy.deepcopy(result)
        bad_digest["request_digest"] = "sha256:" + ("b" * 64)
        with self.assertRaisesRegex(ValueError, "request digest mismatch"):
            validate_operator_result(bad_digest, request)

        bad_count = copy.deepcopy(result)
        bad_count["children_completed"] = 1
        with self.assertRaisesRegex(ValueError, "completed count mismatch"):
            validate_operator_result(bad_count, request)


if __name__ == "__main__":
    unittest.main()
