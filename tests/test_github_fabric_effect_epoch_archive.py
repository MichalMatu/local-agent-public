"""Pure archive checkpoint tests: never exercise a browser or GitHub writer."""

from __future__ import annotations

import copy
import unittest

from local_agent.conversation import github_fabric_effect_epoch_archive as archive
from local_agent.conversation import github_fabric_effect_intents as effects
from local_agent.conversation import github_fabric_parent_ownership as parent_policy
from local_agent.conversation.github_fabric_receipt_aggregation import (
    aggregate_private_child_receipts,
)
from tests.test_github_fabric_receipt_aggregation import (
    URL_A, URL_B, dispatch, evidence,
)


class PrivateEffectEpochArchivePolicyTests(unittest.TestCase):
    def setUp(self):
        self.dispatch = dispatch()
        _, self.active = parent_policy.propose_acquisition(self.dispatch, "a" * 32)
        self.first = self.dispatch["children"][0]["request_id"]
        self.second = self.dispatch["children"][1]["request_id"]
        self.head = "b" * 40

    @property
    def completed(self):
        return {
            **self.active,
            "phase": "completed",
            "completion": "verified",
        }

    def summary(self, *, both=True):
        inputs = {self.first: evidence(self.dispatch, 0, url=URL_A)}
        if both:
            inputs[self.second] = evidence(self.dispatch, 1, url=URL_B)
        return {
            "source_head_sha": self.head,
            **aggregate_private_child_receipts(self.dispatch, inputs),
        }

    def verified_ledger(self):
        first = effects.propose_send_intent(self.active, self.dispatch, self.first)
        first_done = effects.propose_result_verification(
            self.active, self.dispatch, self.first, self.summary(both=False),
            existing=first,
        )
        second = effects.propose_send_intent(
            self.active, self.dispatch, self.second, existing=first_done
        )
        return effects.propose_result_verification(
            self.active, self.dispatch, self.second, self.summary(),
            existing=second,
        )

    def propose(self, *, parent=None, ledger=None, summary=None, head=None):
        return archive.propose_completed_archive(
            self.completed if parent is None else parent,
            self.dispatch, self.verified_ledger() if ledger is None else ledger,
            pinned_summary=self.summary() if summary is None else summary,
            source_head_sha=self.head if head is None else head,
        )

    def test_archives_only_entire_verified_epoch_without_authority(self):
        record = self.propose()
        self.assertEqual(record["ledger"]["revision"], 4)
        self.assertEqual(len(record["ledger"]["effects"]), 2)
        self.assertEqual(record["source_head_sha"], self.head)
        self.assertEqual(record["receipt_summary"], self.summary())
        self.assertTrue(record["receipt_summary_digest"].startswith("sha256:"))
        self.assertFalse(record["browser_send_authorized"])
        self.assertEqual(archive.validate_archive(
            record, self.completed, self.dispatch
        ), record)
        self.assertEqual(archive.archive_projection(
            record, self.completed, self.dispatch
        )["browser_send_authorized"], False)
        self.assertEqual(
            archive.archive_path(self.active["id"], 1),
            archive.ARCHIVE_ROOT + self.active["id"] + "/0001.json",
        )
        self.assertNotIn("bootstrap_text", repr(record))
        self.assertNotIn("read_token", repr(record))

    def test_active_parent_does_not_archive_even_if_results_complete(self):
        with self.assertRaisesRegex(PermissionError, "completed parent"):
            self.propose(parent=self.active)

    def test_unknown_and_frozen_effect_never_archivable(self):
        first = effects.propose_send_intent(self.active, self.dispatch, self.first)
        for ledger in (first, effects.freeze_unknown(
            self.active, self.dispatch, first
        )):
            with self.subTest(phase=ledger["effects"][-1]["phase"]):
                with self.assertRaisesRegex(PermissionError, "every verified child"):
                    self.propose(ledger=ledger)

    def test_partial_receipts_never_archive(self):
        with self.assertRaisesRegex(PermissionError, "pinned receipts incomplete"):
            self.propose(summary=self.summary(both=False))

    def test_injected_redacted_receipt_fields_denied(self):
        extra = self.summary()
        extra["assistant_text"] = "should not be archived"
        with self.assertRaisesRegex(PermissionError, "pinned receipts incomplete"):
            self.propose(summary=extra)
        extra = self.summary()
        extra["completed"] = True
        with self.assertRaisesRegex(PermissionError, "pinned receipts incomplete"):
            self.propose(summary=extra)

    def test_foreign_or_bad_head_receipts_do_not_archive(self):
        with self.assertRaisesRegex(PermissionError, "pinned receipts incomplete"):
            self.propose(head="c" * 40)
        with self.assertRaisesRegex(PermissionError, "pinned receipts incomplete"):
            self.propose(head="bad")
        summary = self.summary()
        summary["children"][0]["child_request_id"] = self.second
        with self.assertRaisesRegex(PermissionError, "child receipt mismatch"):
            self.propose(summary=summary)

    def test_receipt_snapshot_is_embedded_redacted_and_bound_by_digest(self):
        record = self.propose()
        self.assertNotIn("assistant_text", repr(record))
        self.assertNotIn("child_conversation_url", repr(record))
        tampered = copy.deepcopy(record)
        tampered["receipt_summary"]["children"][0]["state"] = "pending"
        with self.assertRaisesRegex(PermissionError, "child receipt mismatch"):
            archive.validate_archive(tampered, self.completed, self.dispatch)
        tampered = copy.deepcopy(record)
        tampered["receipt_summary"]["children"][0]["extra"] = "untrusted"
        with self.assertRaisesRegex(PermissionError, "child receipt mismatch"):
            archive.validate_archive(tampered, self.completed, self.dispatch)
        tampered = copy.deepcopy(record)
        tampered["receipt_summary_digest"] = "sha256:" + "0" * 64
        with self.assertRaisesRegex(ValueError, "evidence mismatch"):
            archive.validate_archive(tampered, self.completed, self.dispatch)
        tampered = copy.deepcopy(record)
        tampered["receipt_summary"]["extra"] = "unaudited"
        with self.assertRaisesRegex(PermissionError, "pinned receipts incomplete"):
            archive.validate_archive(tampered, self.completed, self.dispatch)
        tampered = copy.deepcopy(record)
        tampered["source_head_sha"] = "c" * 40
        with self.assertRaisesRegex(PermissionError, "pinned receipts incomplete"):
            archive.validate_archive(tampered, self.completed, self.dispatch)

    def test_original_records_mutation_does_not_change_proposed_archive(self):
        ledger = self.verified_ledger()
        summary = self.summary()
        archive_record = self.propose(ledger=ledger, summary=summary)
        ledger["effects"][0]["phase"] = "send_unknown"
        summary["children"][0]["state"] = "pending"
        self.assertEqual(
            archive.validate_archive(archive_record, self.completed, self.dispatch),
            archive_record,
        )
        self.assertEqual(
            archive_record["receipt_summary"]["children"][0]["state"], "completed"
        )
        self.assertTrue(all(
            item["phase"] == "result_verified"
            for item in archive_record["ledger"]["effects"]
        ))

    def test_sha_digest_cannot_be_forged_without_revalidation(self):
        record = self.propose()
        malicious = copy.deepcopy(record)
        malicious["ledger_digest"] = "sha256:" + "0" * 64
        with self.assertRaisesRegex(ValueError, "evidence mismatch"):
            archive.validate_archive(malicious, self.completed, self.dispatch)
        malicious = copy.deepcopy(record)
        malicious["ledger"]["effects"][0]["result_source_head_sha"] = "d" * 40
        with self.assertRaisesRegex(ValueError, "evidence mismatch"):
            archive.validate_archive(malicious, self.completed, self.dispatch)
        malicious = copy.deepcopy(record)
        malicious["browser_send_authorized"] = True
        with self.assertRaisesRegex(ValueError, "header invalid"):
            archive.validate_archive(malicious, self.completed, self.dispatch)

    def test_epoch_identity_drift_and_unbounded_epochs_denied(self):
        record = self.propose()
        with self.assertRaises(ValueError):
            archive.validate_archive(record, {
                **self.completed, "fence_epoch": 2,
            }, self.dispatch)
        for epoch in (True, 0, 33, "1"):
            with self.assertRaises(ValueError):
                archive.archive_path(self.active["id"], epoch)

    def test_candidate_has_no_io_or_migration_methods(self):
        for name in ("archive_to_github", "publish", "remove_current",
                     "authorize_send", "takeover", "write"):
            self.assertFalse(hasattr(archive, name))
        self.assertFalse(self.propose()["browser_send_authorized"])


if __name__ == "__main__":
    unittest.main()
