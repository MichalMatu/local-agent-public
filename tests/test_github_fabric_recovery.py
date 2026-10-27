"""Cold GitHub-only reconstruction of approved synthetic Fabric state."""

from __future__ import annotations

import copy
import json
import unittest

from local_agent.conversation import github_fabric_claims as claims
from local_agent.conversation import github_fabric_claims_github as claims_writer
from local_agent.conversation import github_fabric_github as dispatch_writer
from local_agent.conversation import github_fabric_publication as dispatch_publication
from local_agent.conversation import github_fabric_recovery as recovery
from tests.test_github_fabric_claims import AtomicGitDataAPI
from tests.test_github_fabric_dispatch import admitted_children, operator_request


class GithubFabricSyntheticColdRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.api = AtomicGitDataAPI()
        self.operator = operator_request()
        self.children = admitted_children()

    def publish_dispatch(self):
        return dispatch_writer.publish_synthetic_fixture(
            self.operator, self.children, api=self.api, enabled=True
        )

    def publish_claims(self):
        return claims_writer.publish_synthetic_claims(
            self.operator, self.children, api=self.api, enabled=True
        )

    def recover(self, **kwargs):
        args = {"api": self.api, "enabled": True}
        args.update(kwargs)
        return recovery.recover_public_synthetic_snapshot(
            self.operator, self.children, **args
        )

    def test_disabled_and_private_data_refused_before_remote_io(self):
        with self.assertRaises(PermissionError):
            recovery.recover_public_synthetic_snapshot(
                self.operator, self.children, api=self.api
            )
        self.children[0]["scope"]["summary"] = "non-public user's work"
        with self.assertRaises(PermissionError):
            self.recover()
        self.assertEqual(self.api.operations, [])

    def test_cold_recovery_joins_exact_same_origin_without_local_campaign(self):
        self.publish_dispatch()
        self.publish_claims()
        operations = len(self.api.operations)
        remote_head = self.api.head
        first = self.recover()
        self.assertEqual(first.source_head_sha, remote_head)
        self.assertEqual(first.source_kind, "public_synthetic_observation_only")
        self.assertEqual(first.operator_request_id, self.operator["id"])
        self.assertEqual(first.workflow_id, self.operator["workflow_id"])
        self.assertEqual(first.parent_conversation_url, self.operator["parent_conversation_url"])
        self.assertEqual(len(first.children), 2)
        self.assertTrue(all(child.lifecycle == recovery.UNCONFIRMED for child in first.children))
        self.assertEqual(
            [child.child_request_id for child in first.children],
            ["child-verify", "child-research"],
        )
        self.assertEqual(len(self.api.commits), 3)  # dispatch record, index, claim transaction
        remote_writes = [
            op for op in self.api.operations[operations:] if op[0] != "GET"
        ]
        self.assertEqual(remote_writes, [])
        # Simulate a new process/profile with no local storage or prior cache.
        second = self.recover()
        self.assertEqual(second, first)
        self.assertEqual(self.api.head, remote_head)
        self.assertEqual(len(self.api.commits), 3)

    def test_dispatch_without_claims_is_unconfirmed_not_eligible_for_send(self):
        self.publish_dispatch()
        with self.assertRaisesRegex(ValueError, "claim index is missing"):
            self.recover()
        self.assertEqual(len(self.api.commits), 2)

    def test_claims_without_indexed_dispatch_are_not_recovered(self):
        self.publish_claims()
        with self.assertRaisesRegex(ValueError, "dispatch index is missing"):
            self.recover()

    def test_any_dangling_index_is_rejected_even_for_other_claim(self):
        self.publish_dispatch()
        self.publish_claims()
        index = json.loads(self.api.snapshots[self.api.head][claims.INDEX_PATH])
        path = claims.claim_path(index["claim_ids"][0])
        self.api.snapshots[self.api.head].pop(path)
        with self.assertRaisesRegex(ValueError, "indexed semantic claim is missing"):
            self.recover()

    def test_claim_mutation_at_known_identity_fails_closed(self):
        self.publish_dispatch()
        self.publish_claims()
        first = claims.build_claims(self.operator, self.children)[0]
        path = claims.claim_path(first["id"])
        changed = copy.deepcopy(first)
        changed["bootstrap_digest"] = "sha256:" + "f" * 64
        self.api.snapshots[self.api.head][path] = json.dumps(changed)
        with self.assertRaisesRegex(ValueError, "claim conflicts"):
            self.recover()

    def test_dispatch_mutation_or_missing_record_is_rejected(self):
        dispatch = self.publish_dispatch()
        self.publish_claims()
        path = dispatch_publication.RECORD_ROOT + dispatch.dispatch_id + ".json"
        stored = json.loads(self.api.snapshots[self.api.head][path])
        stored["children"][0]["spawn"]["bootstrap_text"] += "\nchanged"
        self.api.snapshots[self.api.head][path] = json.dumps(stored)
        with self.assertRaisesRegex(ValueError, "same-id dispatch conflict"):
            self.recover()
        self.api.snapshots[self.api.head].pop(path)
        with self.assertRaisesRegex(ValueError, "indexed dispatch record is missing"):
            self.recover()

    def test_malformed_index_denied_api_and_invalid_ref_are_rejected(self):
        self.publish_dispatch()
        self.publish_claims()
        self.api.corrupt_path = claims.INDEX_PATH
        with self.assertRaises(ValueError):
            self.recover()
        self.api.corrupt_path = None
        self.api.deny_next_request = True
        with self.assertRaises(dispatch_writer.GithubFabricHTTPError) as ctx:
            self.recover()
        self.assertEqual(ctx.exception.status, 403)
        self.assertEqual(len(self.api.commits), 3)

    def test_observation_does_not_infer_ack_child_url_or_terminal_result(self):
        self.publish_dispatch()
        self.publish_claims()
        found = self.recover()
        self.assertNotIn("ack", found.__dataclass_fields__)
        self.assertNotIn("result", found.__dataclass_fields__)
        for child in found.children:
            self.assertFalse(hasattr(child, "child_conversation_url"))
            self.assertFalse(hasattr(child, "send_authorized"))


if __name__ == "__main__":
    unittest.main()
