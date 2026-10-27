"""Synthetic multi-child evidence tests; no private GitHub or browser access."""

from __future__ import annotations

import copy
import unittest
from unittest import mock

from local_agent.conversation.github_fabric_receipt_aggregation import (
    aggregate_owner_bound_private_child_receipts,
    aggregate_private_child_receipts,
)


PARENT = "https://chatgpt.com/c/6ac6840c-eaec-83ed-bde2-6971e9ffa220"
URL_A = "https://chatgpt.com/c/6ac6840c-eaec-83ed-bde2-6971e9ffa221"
URL_B = "https://chatgpt.com/c/6ac6840c-eaec-83ed-bde2-6971e9ffa222"


def dispatch() -> dict:
    return {
        "schema_version": 1, "operation": "delegate",
        "id": "fabric-" + "a" * 32,
        "request_id": "operator-batch-001",
        "request_digest": "sha256:" + "b" * 64,
        "campaign_id": "cf-" + "c" * 16,
        "parent_conversation_url": PARENT,
        "children": [
            {
                "id": f"child-{index}", "request_id": f"child-{index}",
                "role": role,
                "spawn": {
                    "schema_version": 1, "transaction_id": "spawn-" + digit * 64,
                    "child_request_digest": "sha256:" + "e" * 64,
                    "bootstrap_digest": "sha256:" + "f" * 64,
                    "bootstrap_text": "SENSITIVE PRIVATE PROMPT MUST NOT BE EXPOSED",
                },
            }
            for index, (role, digit) in enumerate(
                (("research", "1"), ("verification", "2")), start=1
            )
        ],
    }


def evidence(batch: dict, index: int, *, url: str) -> dict:
    child = batch["children"][index]
    common = {
        "schema_version": 1,
        "dispatch_id": batch["id"],
        "child_request_id": child["request_id"],
        "spawn_transaction_id": child["spawn"]["transaction_id"],
    }
    return {
        "claim": {
            **common, "kind": "browser_child_claim",
            "parent_conversation_url": PARENT, "owner_id": "a" * 32,
        },
        "ack": {
            **common, "kind": "browser_spawn_ack",
            "bootstrap_digest": child["spawn"]["bootstrap_digest"],
            "child_conversation_url": url,
        },
        "result": {
            **common, "kind": "browser_terminal_result",
            "child_conversation_url": url,
            "assistant_identity": "assistant-opaque-001",
            "assistant_text": "SENSITIVE PRIVATE ANSWER",
        },
    }


class PrivateMultiChildAggregationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.batch = dispatch()
        self.a = evidence(self.batch, 0, url=URL_A)
        self.b = evidence(self.batch, 1, url=URL_B)

    def aggregate(self, entries: dict) -> dict:
        return aggregate_private_child_receipts(self.batch, entries)

    def test_empty_and_claim_only_are_not_assumed_unsent(self) -> None:
        result = self.aggregate({})
        self.assertEqual(result["state"], "pending")
        self.assertEqual(result["total"], 2)
        self.assertFalse(result["requires_reconciliation"])

        claim_only = self.aggregate({"child-2": {"claim": self.b["claim"]}})
        self.assertEqual(claim_only["state"], "in_progress")
        self.assertEqual(
            [child["state"] for child in claim_only["children"]],
            ["pending", "claim_only_unknown"],
        )
        self.assertTrue(claim_only["requires_reconciliation"])

    def test_independent_out_of_order_child_evidence(self) -> None:
        data = {"child-2": copy.deepcopy(self.b), "child-1": copy.deepcopy(self.a)}
        result = self.aggregate(data)
        self.assertEqual(result["state"], "completed")
        self.assertEqual(result["completed"], 2)
        self.assertFalse(result["requires_reconciliation"])
        self.assertEqual(
            [item["child_request_id"] for item in result["children"]],
            ["child-1", "child-2"],
        )
        self.assertNotIn("SENSITIVE", repr(result))

        del data["child-2"]["result"]
        partial = self.aggregate(data)
        self.assertEqual(partial["state"], "partial")
        self.assertEqual(partial["completed"], 1)
        self.assertEqual(
            [item["state"] for item in partial["children"]],
            ["completed", "acknowledged"],
        )

    def test_partial_failure_does_not_invalidate_other_child(self) -> None:
        broken = copy.deepcopy(self.b)
        broken["result"]["child_conversation_url"] = URL_A
        result = self.aggregate({"child-1": self.a, "child-2": broken})
        self.assertEqual(result["state"], "partial_failure")
        self.assertEqual(result["completed"], 1)
        self.assertTrue(result["requires_reconciliation"])
        self.assertEqual(result["children"][1]["reason"], "child_url_mismatch")

    def test_orphan_ack_and_result_fail_closed(self) -> None:
        orphan_ack = self.aggregate({"child-1": {"ack": self.a["ack"]}})
        self.assertEqual(orphan_ack["state"], "invalid_evidence")
        self.assertEqual(orphan_ack["children"][0]["reason"], "ack_without_claim")
        orphan_result = self.aggregate({
            "child-1": {"claim": self.a["claim"], "result": self.a["result"]}
        })
        self.assertEqual(orphan_result["children"][0]["reason"], "result_without_ack")

    def test_cross_child_chat_reuse_fails_both(self) -> None:
        reused = copy.deepcopy(self.b)
        reused["ack"]["child_conversation_url"] = URL_A
        reused["result"]["child_conversation_url"] = URL_A
        result = self.aggregate({"child-1": self.a, "child-2": reused})
        self.assertEqual(result["state"], "invalid_evidence")
        self.assertEqual(
            [item["reason"] for item in result["children"]],
            ["shared_child_url", "shared_child_url"],
        )

    def test_wrong_transaction_digest_owner_and_extra_keys(self) -> None:
        samples = []
        wrong_transaction = copy.deepcopy(self.a)
        wrong_transaction["ack"]["spawn_transaction_id"] = "spawn-" + "9" * 64
        samples.append(wrong_transaction)
        wrong_digest = copy.deepcopy(self.a)
        wrong_digest["ack"]["bootstrap_digest"] = "sha256:" + "0" * 64
        samples.append(wrong_digest)
        wrong_owner = copy.deepcopy(self.a)
        wrong_owner["claim"]["owner_id"] = "../unsafe"
        samples.append(wrong_owner)
        extra = copy.deepcopy(self.a)
        extra["result"]["arbitrary"] = "injected"
        samples.append(extra)
        for record in samples:
            with self.subTest(record=record):
                result = self.aggregate({"child-1": record})
                self.assertEqual(result["children"][0]["state"], "invalid_evidence")
                self.assertNotIn("SENSITIVE", repr(result))

    def test_owner_bound_action_aggregation_requires_explicit_valid_owner(self) -> None:
        receipts = {"child-1": self.a, "child-2": self.b}
        with self.assertRaises(TypeError):
            aggregate_owner_bound_private_child_receipts(self.batch, receipts)
        valid = aggregate_owner_bound_private_child_receipts(
            self.batch, receipts, owner_id="a" * 32
        )
        self.assertEqual(valid["state"], "completed")
        wrong = aggregate_owner_bound_private_child_receipts(
            self.batch, receipts, owner_id="b" * 32
        )
        self.assertEqual(wrong["state"], "invalid_evidence")
        self.assertTrue(wrong["requires_reconciliation"])
        for invalid_owner in ("invalid-owner", True, None, "", "a" * 31):
            with self.subTest(owner=invalid_owner):
                with self.assertRaisesRegex(
                    ValueError, "owner-bound receipt identity invalid"
                ), mock.patch(
                    "local_agent.conversation.github_fabric_receipt_aggregation."
                    "aggregate_private_child_receipts"
                ) as unbound:
                    aggregate_owner_bound_private_child_receipts(
                        self.batch, receipts, owner_id=invalid_owner
                    )
                unbound.assert_not_called()

    def test_pinned_parent_owner_mismatch_never_counts_completed(self) -> None:
        completed = self.aggregate({"child-1": self.a, "child-2": self.b})
        self.assertEqual(completed["state"], "completed")
        pinned = aggregate_private_child_receipts(
            self.batch, {"child-1": self.a, "child-2": self.b},
            expected_owner_id="a" * 32,
        )
        self.assertEqual(pinned["state"], "completed")
        with_wrong_owner = aggregate_private_child_receipts(
            self.batch, {"child-1": self.a, "child-2": self.b},
            expected_owner_id="b" * 32,
        )
        self.assertEqual(with_wrong_owner["state"], "invalid_evidence")
        self.assertTrue(with_wrong_owner["requires_reconciliation"])
        self.assertEqual(
            [row["reason"] for row in with_wrong_owner["children"]],
            ["owner_id_conflict", "owner_id_conflict"],
        )
        self.assertNotIn("'owner_id':", repr(with_wrong_owner))
        self.assertNotIn("b" * 32, repr(with_wrong_owner))
        for invalid_owner in ("a" * 31, "../wrong", 1, True):
            with self.subTest(owner=invalid_owner), self.assertRaises(ValueError):
                aggregate_private_child_receipts(
                    self.batch, {}, expected_owner_id=invalid_owner
                )

    def test_unknown_child_and_duplicate_request_identity_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "unknown child identity"):
            self.aggregate({"child-outside-dispatch": self.a})
        duplicate = copy.deepcopy(self.batch)
        duplicate["children"][1]["request_id"] = "child-1"
        with self.assertRaisesRegex(ValueError, "duplicated"):
            aggregate_private_child_receipts(duplicate, {})


if __name__ == "__main__":
    unittest.main()
