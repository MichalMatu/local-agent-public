"""Candidate global Send-intent ledger: no Send, GitHub IO, or browser authority."""

from __future__ import annotations

import copy
import unittest

from local_agent.conversation import github_fabric_effect_intents as effect
from local_agent.conversation import github_fabric_parent_ownership as ownership
from local_agent.conversation.github_fabric_receipt_aggregation import (
    aggregate_private_child_receipts,
)
from tests.test_github_fabric_receipt_aggregation import URL_A, URL_B, dispatch, evidence


class PrivateParentSendIntentTests(unittest.TestCase):
    def setUp(self):
        self.dispatch = dispatch()
        _, self.owner = ownership.propose_acquisition(self.dispatch, "a" * 32)
        self.first = self.dispatch["children"][0]["request_id"]
        self.second = self.dispatch["children"][1]["request_id"]

    def summary(self, *, first=True, second=False):
        inputs = {}
        if first:
            inputs[self.first] = evidence(self.dispatch, 0, url=URL_A)
        if second:
            inputs[self.second] = evidence(self.dispatch, 1, url=URL_B)
        return {
            "source_head_sha": "b" * 40,
            **aggregate_private_child_receipts(self.dispatch, inputs),
        }

    def start(self):
        return effect.propose_send_intent(self.owner, self.dispatch, self.first)

    def test_first_send_is_durable_unknown_not_permission(self):
        record = self.start()
        self.assertEqual(record["revision"], 1)
        self.assertEqual(record["effects"][0]["phase"], "send_unknown")
        self.assertEqual(record["effects"][0]["effect_id"],
                         effect.effect_id(self.owner, self.dispatch["children"][0]))
        self.assertEqual(effect.recovery_projection(self.owner, self.dispatch, record), {
            "parent_id": self.owner["id"], "revision": 1,
            "status": "reconcile_only_no_send_replay", "browser_send_authorized": False,
        })
        self.assertEqual(effect.effect_path(self.owner["id"]).split("/")[-1],
                         self.owner["id"] + ".json")
        self.assertNotIn("bootstrap_text", repr(record))
        self.assertNotIn("assistant_text", repr(record))
        self.assertNotIn("read_token", repr(record))

    def test_replay_after_restart_or_wrong_order_never_arms(self):
        first = self.start()
        restarted = copy.deepcopy(first)
        with self.assertRaisesRegex(PermissionError, "duplicate, skipped or out of order"):
            effect.propose_send_intent(self.owner, self.dispatch, self.first,
                                      existing=restarted)
        with self.assertRaisesRegex(PermissionError, "predecessor Send unknown"):
            effect.propose_send_intent(self.owner, self.dispatch, self.second,
                                      existing=restarted)
        with self.assertRaisesRegex(PermissionError, "duplicate, skipped or out of order"):
            effect.propose_send_intent(self.owner, self.dispatch, self.second)

    def test_result_only_if_matching_pinned_completed_evidence(self):
        first = self.start()
        verified = effect.propose_result_verification(
            self.owner, self.dispatch, self.first, self.summary(), existing=first
        )
        self.assertEqual(verified["revision"], 2)
        self.assertEqual(verified["effects"][0]["phase"], "result_verified")
        self.assertEqual(verified["effects"][0]["result_source_head_sha"], "b" * 40)
        second = effect.propose_send_intent(
            self.owner, self.dispatch, self.second, existing=verified
        )
        self.assertEqual(second["revision"], 3)
        self.assertEqual(len(second["effects"]), 2)
        self.assertEqual(second["effects"][1]["phase"], "send_unknown")
        self.assertFalse(effect.recovery_projection(
            self.owner, self.dispatch, second
        )["browser_send_authorized"])
        second_done = effect.propose_result_verification(
            self.owner, self.dispatch, self.second,
            self.summary(second=True), existing=second
        )
        self.assertEqual(second_done["revision"], 4)
        self.assertEqual([e["phase"] for e in second_done["effects"]],
                         ["result_verified", "result_verified"])
        with self.assertRaisesRegex(PermissionError, "duplicate, skipped or out of order"):
            effect.propose_send_intent(
                self.owner, self.dispatch, self.second, existing=second_done
            )

    def test_partial_claim_only_wrong_digest_or_identity_denied(self):
        first = self.start()
        partial = self.summary(first=False)
        with self.assertRaisesRegex(PermissionError, "independent terminal"):
            effect.propose_result_verification(
                self.owner, self.dispatch, self.first, partial, existing=first
            )
        variations = []
        for mutate in (
            lambda x: x["children"][0].update({"child_request_id": self.second}),
            lambda x: x["children"][0].update({"state": "acknowledged"}),
            lambda x: x["children"][0].update({"reason": "not_verified"}),
            lambda x: x.update({"source_head_sha": "bad"}),
            lambda x: x.update({"dispatch_id": "fabric-" + "f" * 32}),
        ):
            item = self.summary()
            mutate(item)
            variations.append(item)
        for summary in variations:
            with self.subTest(summary=summary):
                with self.assertRaises(PermissionError):
                    effect.propose_result_verification(
                        self.owner, self.dispatch, self.first, summary, existing=first
                    )

    def test_duplicate_forged_or_out_of_order_effect_rejected(self):
        first = self.start()
        variants = [
            {"revision": True},
            {"revision": 300},
            {"parent_id": "parent-" + "0" * 32},
            {"fence_epoch": 2},
            {"owner_id": "f" * 32},
            {"effects": [first["effects"][0], first["effects"][0]]},
        ]
        for change in variants:
            with self.subTest(change=change):
                malformed = copy.deepcopy(first)
                malformed.update(change)
                with self.assertRaises(ValueError):
                    effect.validate_ledger(malformed, self.owner, self.dispatch)
        forged = copy.deepcopy(first)
        forged["effects"][0]["effect_id"] = "effect-" + "0" * 32
        with self.assertRaisesRegex(ValueError, "identity"):
            effect.validate_ledger(forged, self.owner, self.dispatch)
        forged = copy.deepcopy(first)
        forged["effects"][0]["phase"] = "result_verified"
        forged["effects"][0]["result_source_head_sha"] = "b" * 40
        with self.assertRaises(ValueError):
            effect.validate_ledger(forged, self.owner, self.dispatch)
        forged = copy.deepcopy(first)
        forged["effects"][0]["result_source_head_sha"] = "b" * 40
        with self.assertRaises(ValueError):
            effect.validate_ledger(forged, self.owner, self.dispatch)

    def test_frozen_effect_stays_blocked_and_never_released(self):
        first = self.start()
        frozen = effect.freeze_unknown(self.owner, self.dispatch, first)
        self.assertEqual(frozen["effects"][0]["phase"], "frozen_unknown")
        self.assertEqual(frozen["revision"], first["revision"] + 1)
        self.assertEqual(effect.freeze_unknown(self.owner, self.dispatch, frozen), frozen)
        self.assertEqual(effect.recovery_projection(
            self.owner, self.dispatch, frozen
        )["status"], "blocked_unknown_no_takeover")
        with self.assertRaisesRegex(PermissionError, "predecessor Send unknown"):
            effect.propose_send_intent(self.owner, self.dispatch, self.second,
                                      existing=frozen)
        with self.assertRaisesRegex(PermissionError, "verification replay"):
            effect.propose_result_verification(
                self.owner, self.dispatch, self.first, self.summary(), existing=frozen
            )
        with self.assertRaisesRegex(PermissionError, "duplicate"):
            effect.propose_send_intent(
                self.owner, self.dispatch, self.first, existing=frozen
            )

    def test_owner_epoch_drift_and_completed_parent_denied(self):
        first = self.start()
        _, other_owner = ownership.propose_acquisition(
            self.dispatch, "c" * 32
        )
        with self.assertRaises(ValueError):
            effect.validate_ledger(first, other_owner, self.dispatch)
        completed_parent = {
            **self.owner, "phase": "completed", "completion": "verified"
        }
        with self.assertRaisesRegex(PermissionError, "active"):
            effect.propose_send_intent(
                completed_parent, self.dispatch, self.first
            )
        with self.assertRaisesRegex(PermissionError, "active"):
            effect.propose_result_verification(
                completed_parent, self.dispatch, self.first, self.summary(),
                existing=first
            )

    def test_empty_recovery_and_wrong_dispatch_rejected(self):
        self.assertEqual(effect.recovery_projection(
            self.owner, self.dispatch, None
        )["status"], "unarmed")
        first = self.start()
        competing = copy.deepcopy(self.dispatch)
        competing["id"] = "fabric-" + "f" * 32
        with self.assertRaises(ValueError):
            effect.validate_ledger(first, self.owner, competing)


if __name__ == "__main__":
    unittest.main()
