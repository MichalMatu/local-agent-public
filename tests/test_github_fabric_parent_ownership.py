"""Synthetic parent ownership adversarial policy tests; no network or browser."""

from __future__ import annotations

import copy
import unittest

from local_agent.conversation.github_fabric_parent_ownership import (
    freeze_uncertain, parent_id, path, propose_acquisition, propose_completion, validate,
)
from local_agent.conversation.github_fabric_receipt_aggregation import (
    aggregate_private_child_receipts,
)
from tests.test_github_fabric_receipt_aggregation import (
    URL_A, URL_B, dispatch, evidence,
)


OWNER_1 = "a" * 32
OWNER_2 = "b" * 32
PINNED_SHA = "c" * 40


def complete_summary(batch):
    summary = aggregate_private_child_receipts(batch, {
        "child-1": evidence(batch, 0, url=URL_A),
        "child-2": evidence(batch, 1, url=URL_B),
    })
    return {"source_head_sha": PINNED_SHA, **summary}


class ParentOwnershipPolicyTests(unittest.TestCase):
    def setUp(self):
        self.batch = dispatch()
        self.competing = copy.deepcopy(self.batch)
        self.competing["id"] = "fabric-" + "d" * 32

    def test_one_parent_one_controller_and_no_implicit_takeover(self):
        step, owned = propose_acquisition(self.batch, OWNER_1)
        self.assertEqual(step, "propose_cas")
        self.assertEqual(owned["fence_epoch"], 1)
        self.assertEqual(owned["id"], parent_id(self.batch["parent_conversation_url"]))
        self.assertEqual(path(owned["id"]).split("/")[-1], owned["id"] + ".json")
        self.assertNotIn("bootstrap_text", repr(owned))
        self.assertNotIn("browser_send_authorized", owned)

        again, same = propose_acquisition(self.batch, OWNER_1, existing=owned)
        self.assertEqual(again, "replay")
        self.assertEqual(same, owned)

        with self.assertRaisesRegex(PermissionError, "already owned"):
            propose_acquisition(self.batch, OWNER_2, existing=owned)
        with self.assertRaisesRegex(PermissionError, "already owned"):
            propose_acquisition(self.competing, OWNER_1, existing=owned)

    def test_claim_only_or_partial_never_releases_parent(self):
        _, owner = propose_acquisition(self.batch, OWNER_1)
        receipts = complete_summary(self.batch)
        partial = copy.deepcopy(receipts)
        partial["children"][1]["state"] = "claim_only_unknown"
        partial["state"] = "partial"
        partial["completed"] = 1
        partial["requires_reconciliation"] = True
        with self.assertRaisesRegex(PermissionError, "requires pinned all-child receipts"):
            propose_completion(owner, self.batch, partial)
        unknown = freeze_uncertain(owner)
        self.assertEqual(unknown["phase"], "unknown_frozen")
        self.assertEqual(unknown["completion"], "unresolved")
        self.assertEqual(freeze_uncertain(unknown), unknown)
        for candidate in (self.batch, self.competing):
            with self.subTest(candidate=candidate["id"]):
                with self.assertRaisesRegex(PermissionError, "manual reconciliation"):
                    propose_acquisition(candidate, OWNER_2, existing=unknown)
        with self.assertRaisesRegex(PermissionError, "identity mismatch"):
            propose_completion(unknown, self.batch, receipts)

    def test_full_private_summary_allows_proposed_epoch_increment_only(self):
        _, first = propose_acquisition(self.batch, OWNER_1)
        completed = propose_completion(first, self.batch, complete_summary(self.batch))
        self.assertEqual(completed["phase"], "completed")
        self.assertEqual(completed["fence_epoch"], 1)
        with self.assertRaisesRegex(ValueError, "cannot become unknown"):
            freeze_uncertain(completed)
        step, successor = propose_acquisition(self.competing, OWNER_2, existing=completed)
        self.assertEqual(step, "propose_cas")
        self.assertEqual(successor["fence_epoch"], 2)
        self.assertEqual(successor["owner_id"], OWNER_2)
        self.assertEqual(successor["dispatch_id"], self.competing["id"])
        with self.assertRaisesRegex(PermissionError, "already owned"):
            propose_acquisition(self.batch, OWNER_1, existing=successor)
        with self.assertRaisesRegex(PermissionError, "may not be rearmed"):
            propose_acquisition(self.batch, OWNER_2, existing=completed)
        with self.assertRaisesRegex(PermissionError, "distinct owner identity"):
            propose_acquisition(self.competing, OWNER_1, existing=completed)

    def test_missing_invalid_or_forged_completion_fails_closed(self):
        _, first = propose_acquisition(self.batch, OWNER_1)
        summary = complete_summary(self.batch)
        variants = []
        for mutate in (
            lambda x: x.update({"source_head_sha": "not-a-sha"}),
            lambda x: x.update({"dispatch_id": self.competing["id"]}),
            lambda x: x.update({"requires_reconciliation": True}),
            lambda x: x["children"][0].update({"state": "acknowledged"}),
            lambda x: x["children"][1].update({"child_request_id": "child-1"}),
            lambda x: x["children"][0].update({"reason": "unknown"}),
        ):
            modified = copy.deepcopy(summary)
            mutate(modified)
            variants.append(modified)
        for record in variants:
            with self.subTest(record=record):
                with self.assertRaisesRegex(PermissionError, "pinned all-child receipts"):
                    propose_completion(first, self.batch, record)
        with self.assertRaisesRegex(PermissionError, "completion identity mismatch"):
            propose_completion(first, self.competing, summary)
        with self.assertRaisesRegex(ValueError, "parent fence fields invalid"):
            validate({**first, "token": "must-not-be-present"})
        with self.assertRaisesRegex(ValueError, "parent fence"):
            validate({**first, "fence_epoch": 0})
        with self.assertRaisesRegex(ValueError, "parent fence"):
            validate({**first, "phase": []})
        with self.assertRaisesRegex(ValueError, "owner invalid"):
            propose_acquisition(self.batch, "../other-controller")
        with self.assertRaisesRegex(ValueError, "parent fence identity invalid"):
            path("../traversal")


if __name__ == "__main__":
    unittest.main()
