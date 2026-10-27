"""Private candidate effect-intent CAS: isolated Git Data races, no browser work."""

from __future__ import annotations

import json
import unittest

from local_agent.conversation import github_fabric_effect_intents as effects
from local_agent.conversation import github_fabric_effect_intents_github as effect_git
from local_agent.conversation import github_fabric_github as git
from local_agent.conversation import github_fabric_parent_ownership_github as ownership
from local_agent.conversation import github_fabric_private_github as private_git
from local_agent.conversation import github_fabric_private_live as live
from local_agent.conversation import github_fabric_private_publication as catalog
from tests.test_github_fabric_dispatch import admitted_children, operator_request
from tests.test_github_fabric_parent_ownership_github import ParentGitDataAPI
from tests.test_github_fabric_receipt_aggregation import URL_A, URL_B, evidence


class PrivateEffectIntentCASTests(unittest.TestCase):
    def setUp(self):
        self.api = ParentGitDataAPI()
        workflows = json.dumps({"schema_version": 1, "workflow_ids": ["workflow-001"]})
        self.api.snapshots[self.api.head][catalog.WORKFLOWS_PATH] = workflows
        self.api.trees["b" * 40][catalog.WORKFLOWS_PATH] = workflows
        self.staged = live.stage_private_dispatch(
            operator_request(), admitted_children(),
            enabled=True, writer_authorized=True, api=self.api,
        )
        self.dispatch_id = self.staged.dispatch_id
        self.owner = "1" * 32
        self.lease = ownership.acquire_parent_candidate(
            self.dispatch_id, self.owner,
            enabled=True, writer_authorized=True, api=self.api,
        )
        self.dispatch = json.loads(
            self.api.snapshots[self.api.head][catalog.dispatch_path(self.dispatch_id)]
        )
        self.first = self.dispatch["children"][0]["request_id"]
        self.second = self.dispatch["children"][1]["request_id"]

    def arm(self, child=None, **options):
        return effect_git.record_candidate_send_intent(
            self.dispatch_id, self.owner, self.lease.fence_epoch,
            child or self.first,
            enabled=True, writer_authorized=True, api=self.api, **options,
        )

    def verify(self, child=None):
        return effect_git.verify_candidate_child_result(
            self.dispatch_id, self.owner, self.lease.fence_epoch,
            child or self.first,
            enabled=True, writer_authorized=True, api=self.api,
        )

    def ledger(self):
        return json.loads(self.api.snapshots[self.api.head][
            effects.effect_path(self.lease.parent_id)
        ])

    def add_receipts(self, index):
        child = self.dispatch["children"][index]
        source = evidence(
            self.dispatch, index, url=URL_A if index == 0 else URL_B
        )
        source["claim"]["owner_id"] = self.owner
        for kind, payload in source.items():
            path = (
                f"projects/local-agent/workflows/workflow-001/receipts/"
                f"{self.dispatch_id}/{kind}/{child['request_id']}.json"
            )
            self.api.snapshots[self.api.head][path] = json.dumps(payload)
            tree_sha = self.api.commit_trees[self.api.head]
            self.api.trees[tree_sha][path] = json.dumps(payload)

    def test_disabled_before_private_io(self):
        before = len(self.api.operations)
        with self.assertRaisesRegex(PermissionError, "default-disabled"):
            effect_git.record_candidate_send_intent(
                self.dispatch_id, self.owner, 1, self.first, api=self.api
            )
        with self.assertRaisesRegex(PermissionError, "default-disabled"):
            effect_git.verify_candidate_child_result(
                self.dispatch_id, self.owner, 1, self.first, api=self.api
            )
        with self.assertRaisesRegex(PermissionError, "default-disabled"):
            effect_git.read_candidate_effect_status(self.dispatch_id, api=self.api)
        self.assertEqual(len(self.api.operations), before)

    def test_atomic_send_intent_never_authorizes_browser(self):
        before = len(self.api.commits)
        result = self.arm()
        self.assertEqual(result.status, "recorded_no_send")
        self.assertEqual(result.child_request_id, self.first)
        self.assertFalse(result.browser_send_authorized)
        self.assertEqual(len(self.api.commits), before + 1)
        recorded = self.ledger()
        self.assertEqual(recorded["revision"], 1)
        self.assertEqual(recorded["effects"][0]["phase"], "send_unknown")
        self.assertEqual(result.effect_id, recorded["effects"][0]["effect_id"])
        self.assertNotIn("bootstrap_text", repr(recorded))
        self.assertNotIn("assistant_text", repr(recorded))
        self.assertEqual(
            effect_git.read_candidate_effect_status(
                self.dispatch_id, enabled=True, api=self.api,
            )["status"], "reconcile_only_no_send_replay"
        )
        with self.assertRaisesRegex(PermissionError, "duplicate"):
            self.arm()
        self.assertEqual(len(self.api.commits), before + 1)

    def test_foreign_claim_owner_cannot_verify_effect_result(self):
        self.arm()
        self.add_receipts(0)
        path = (
            "projects/local-agent/workflows/workflow-001/receipts/"
            f"{self.dispatch_id}/claim/{self.first}.json"
        )
        forged = json.loads(self.api.snapshots[self.api.head][path])
        forged["owner_id"] = "2" * 32
        self.api.snapshots[self.api.head][path] = json.dumps(forged)
        self.api.trees[self.api.commit_trees[self.api.head]][path] = json.dumps(forged)
        count = len(self.api.commits)
        with self.assertRaisesRegex(PermissionError, "independent terminal"):
            self.verify()
        self.assertEqual(len(self.api.commits), count)
        self.assertEqual(self.ledger()["effects"][0]["phase"], "send_unknown")

    def test_next_child_requires_verified_prior_receipts(self):
        self.arm()
        with self.assertRaisesRegex(PermissionError, "predecessor Send unknown"):
            self.arm(self.second)
        with self.assertRaisesRegex(PermissionError, "independent terminal"):
            self.verify()
        self.add_receipts(0)
        result = self.verify()
        self.assertEqual(result.status, "recorded_no_send")
        self.assertFalse(result.browser_send_authorized)
        self.assertEqual(self.ledger()["effects"][0]["phase"], "result_verified")
        second = self.arm(self.second)
        self.assertEqual(second.status, "recorded_no_send")
        self.assertEqual([e["phase"] for e in self.ledger()["effects"]],
                         ["result_verified", "send_unknown"])
        self.add_receipts(1)
        self.verify(self.second)
        self.assertEqual([e["phase"] for e in self.ledger()["effects"]],
                         ["result_verified", "result_verified"])
        with self.assertRaisesRegex(PermissionError, "duplicate"):
            self.arm(self.second)

    def test_lost_patch_ack_reconciles_without_duplicate_write_or_send(self):
        self.api.fail_after_ref_number = self.api.ref_successes + 1
        before = len(self.api.commits)
        result = self.arm()
        self.assertEqual(result.status, "converged_no_send")
        self.assertFalse(result.browser_send_authorized)
        self.assertEqual(len(self.api.commits), before + 1)
        with self.assertRaises(PermissionError):
            self.arm()

    def test_conflict_after_external_ref_change_requires_reconciliation(self):
        self.api.reject_next_ref = True
        before = len(self.api.commits)
        with self.assertRaises(effect_git.EffectIntentConflict):
            self.arm()
        self.assertEqual(len(self.api.commits), before + 1,
                         "one failed CAS is not blindly retried")
        self.assertNotIn(effects.effect_path(self.lease.parent_id),
                         self.api.snapshots[self.api.head])

    def test_transport_loss_and_unreadable_origin_stays_uncertain(self):
        class OfflineAfterLoss:
            def __init__(self, api):
                self.api = api
                self.lost = False

            def request(self, method, path, body=None):
                if method == "PATCH" and path == private_git.REF_UPDATE_PATH:
                    self.lost = True
                    raise git.GithubFabricTransportError("simulated lost PATCH")
                if self.lost and method == "GET":
                    raise git.GithubFabricTransportError("simulated offline")
                return self.api.request(method, path, body)

        with self.assertRaisesRegex(
            effect_git.EffectIntentOutcomeUncertain, "no automatic replay"
        ):
            effect_git.record_candidate_send_intent(
                self.dispatch_id, self.owner, 1, self.first,
                enabled=True, writer_authorized=True, api=OfflineAfterLoss(self.api)
            )
        self.assertNotIn(effects.effect_path(self.lease.parent_id),
                         self.api.snapshots[self.api.head])

    def test_wrong_owner_epoch_and_corrupt_ledger_are_denied(self):
        with self.assertRaisesRegex(PermissionError, "active owner/epoch mismatch"):
            effect_git.record_candidate_send_intent(
                self.dispatch_id, "2" * 32, 1, self.first,
                enabled=True, writer_authorized=True, api=self.api,
            )
        with self.assertRaisesRegex(PermissionError, "active owner/epoch mismatch"):
            effect_git.record_candidate_send_intent(
                self.dispatch_id, self.owner, 2, self.first,
                enabled=True, writer_authorized=True, api=self.api,
            )
        self.arm()
        path = effects.effect_path(self.lease.parent_id)
        current = json.loads(self.api.snapshots[self.api.head][path])
        current["effects"][0]["effect_id"] = "effect-" + "0" * 32
        self.api.snapshots[self.api.head][path] = json.dumps(current)
        before = len(self.api.commits)
        with self.assertRaisesRegex(ValueError, "identity"):
            self.arm()
        self.assertEqual(len(self.api.commits), before)

    def test_verified_parent_ownership_required_after_completion(self):
        self.arm()
        self.add_receipts(0)
        self.add_receipts(1)
        # Explicit parent completion may be recorded by the existing owner
        # CAS policy, but no future effect may be armed under that epoch.
        ownership.change_parent_candidate(
            self.dispatch_id, self.owner, 1, "complete",
            enabled=True, writer_authorized=True, api=self.api,
        )
        with self.assertRaisesRegex(PermissionError, "active owner/epoch mismatch"):
            self.arm()
        with self.assertRaisesRegex(PermissionError, "active owner/epoch mismatch"):
            self.verify()


if __name__ == "__main__":
    unittest.main()
