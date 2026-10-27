"""Pinned v2 binding reader provenance and redaction tests; synthetic Git only."""

from __future__ import annotations

import json
import unittest

from local_agent.conversation import github_fabric_browser_binding_v2 as policy
from local_agent.conversation import github_fabric_browser_binding_v2_github as writer
from local_agent.conversation import github_fabric_browser_binding_v2_reader as reader
from local_agent.conversation import github_fabric_parent_ownership_github as owners
from local_agent.conversation import github_fabric_private_live as live
from local_agent.conversation import github_fabric_private_publication as catalog
from tests.test_github_fabric_dispatch import admitted_children, child_request, operator_request
from tests.test_github_fabric_parent_ownership_github import ParentGitDataAPI


CHILD_URL = "https://chatgpt.com/c/test-v2-child"


class LineageGitDataAPI(ParentGitDataAPI):
    def request(self, method, path, body=None):
        result = super().request(method, path, body)
        if method == "GET" and path.startswith("/git/commits/"):
            sha = path.rsplit("/", 1)[-1]
            commit = self.commits.get(sha)
            return {
                **result,
                "parents": [
                    {"sha": entry} for entry in commit["parents"]
                ] if commit is not None else [],
            }
        return result

    def _advance_out_of_band(self):
        previous = self.head
        super()._advance_out_of_band()
        self.commits[self.head] = {
            "parents": [previous], "tree": self.commit_trees[self.head],
        }


class PinnedBrowserBindingReaderTests(unittest.TestCase):
    def setUp(self):
        self.api = LineageGitDataAPI()
        workflows = json.dumps({"schema_version": 1, "workflow_ids": ["workflow-001"]})
        self.api.snapshots[self.api.head][catalog.WORKFLOWS_PATH] = workflows
        self.api.trees["b" * 40][catalog.WORKFLOWS_PATH] = workflows
        staged = live.stage_private_dispatch(
            operator_request(), admitted_children(),
            enabled=True, writer_authorized=True, api=self.api,
        )
        self.dispatch_id = staged.dispatch_id
        self.owner_id = "1" * 32
        self.parent = owners.acquire_parent_candidate(
            self.dispatch_id, self.owner_id,
            enabled=True, writer_authorized=True, api=self.api,
        )
        self.dispatch = json.loads(
            self.api.snapshots[self.api.head][catalog.dispatch_path(self.dispatch_id)]
        )
        self.first = self.dispatch["children"][0]["request_id"]
        self.second = self.dispatch["children"][1]["request_id"]
        self.pre_binding_head = self.api.head

    def write(self, child=None, driver="b" * 32):
        return writer.publish_synthetic_browser_binding(
            self.dispatch_id, self.owner_id, 1, child or self.first,
            driver_session_id=driver, enabled=True,
            writer_authorized=True, api=self.api,
        )

    def read(self, child=None, **overrides):
        kw = {"enabled": True, "api": self.api}
        kw.update(overrides)
        return reader.read_synthetic_pinned_browser_binding(
            self.dispatch_id, child or self.first, **kw,
        )

    def path(self, child=None):
        return policy.binding_path(
            self.parent.parent_id, 1, child or self.first,
        )

    def record(self, child=None):
        return json.loads(
            self.api.snapshots[self.api.head][self.path(child)]
        )

    def inject(self, path, value):
        data = json.dumps(value, separators=(",", ":"))
        self.api.snapshots[self.api.head][path] = data
        self.api.trees[self.api.commit_trees[self.api.head]][path] = data

    def test_reader_disabled_before_any_api_and_absent_is_not_send(self):
        count = len(self.api.operations)
        with self.assertRaises(PermissionError):
            self.read(enabled=False)
        with self.assertRaises(PermissionError):
            self.read(api=None)
        self.assertEqual(len(self.api.operations), count)
        projection = self.read()
        self.assertEqual(projection["status"], "absent_no_send")
        self.assertIs(projection["browser_send_authorized"], False)

    def test_pinned_direct_origin_and_replayed_read_never_authorize_send(self):
        result = self.write()
        self.assertEqual(result.status, "recorded_no_send")
        projected = self.read()
        self.assertEqual(projected["binding_origin_head_sha"], self.pre_binding_head)
        self.assertEqual(projected["source_head_sha"], self.api.head)
        self.assertEqual(projected["status"], "pending")
        self.assertFalse(projected["browser_send_authorized"])
        self.assertNotIn("bootstrap_text", str(projected))
        count = len(self.api.commits)
        self.assertEqual(projected, self.read())
        self.assertEqual(len(self.api.commits), count)

    def test_valid_later_unrelated_commit_retains_verified_provenance(self):
        self.write()
        self.api._advance_out_of_band()
        self.assertEqual(self.read()["status"], "pending")

    def test_unindexed_child_denied_before_any_binding_projection(self):
        with self.assertRaisesRegex(PermissionError, "not indexed"):
            self.read("unindexed-child")

    def test_forged_sibling_origin_fails_parent_read(self):
        self.write()
        self.write(self.second, driver="c" * 32)
        forged = self.record(self.second)
        forged["binding_origin_head_sha"] = "9" * 40
        self.inject(self.path(self.second), forged)
        with self.assertRaises(ValueError):
            self.read()

    def test_missing_or_unrelated_origin_is_not_trusted(self):
        self.write()
        record = self.record()
        record["binding_origin_head_sha"] = "9" * 40
        self.inject(self.path(), record)
        with self.assertRaises(ValueError):
            self.read()

    def test_same_record_must_survive_every_pinned_commit(self):
        self.write()
        self.api._advance_out_of_band()
        forged = self.record()
        forged["driver_session_id"] = "c" * 32
        self.inject(self.path(), forged)
        with self.assertRaisesRegex(ValueError, "changed"):
            self.read()

    def test_origin_owner_evidence_cannot_be_replaced_by_claim(self):
        self.write()
        path = owners.policy.path(self.parent.parent_id)
        corrupt = json.loads(self.api.snapshots[self.pre_binding_head][path])
        corrupt["owner_id"] = "2" * 32
        self.api.snapshots[self.pre_binding_head][path] = json.dumps(corrupt)
        with self.assertRaises(ValueError):
            self.read()

    def test_sibling_driver_collision_and_orphan_receipt_fail_closed(self):
        self.write()
        self.write(self.second, driver="c" * 32)
        other = self.record(self.second)
        other["driver_session_id"] = "b" * 32
        self.inject(self.path(self.second), other)
        with self.assertRaisesRegex(ValueError, "reused"):
            self.read()
        # A receipt for an unbound child is never valid completion evidence.
        self.api = LineageGitDataAPI()
        workflows = json.dumps({"schema_version": 1, "workflow_ids": ["workflow-001"]})
        self.api.snapshots[self.api.head][catalog.WORKFLOWS_PATH] = workflows
        self.api.trees["b" * 40][catalog.WORKFLOWS_PATH] = workflows
        staged = live.stage_private_dispatch(
            operator_request(), admitted_children(),
            enabled=True, writer_authorized=True, api=self.api,
        )
        self.dispatch_id = staged.dispatch_id
        self.parent = owners.acquire_parent_candidate(
            self.dispatch_id, self.owner_id,
            enabled=True, writer_authorized=True, api=self.api,
        )
        self.inject(policy.receipt_path(self.dispatch_id, self.first, "claim"), {})
        with self.assertRaisesRegex(ValueError, "orphan"):
            self.read()

    def test_receipt_at_binding_origin_denied_even_if_current_head_is_empty(self):
        self.write()
        early = policy.receipt_path(self.dispatch_id, self.first, "claim")
        self.api.snapshots[self.pre_binding_head][early] = "{}"
        with self.assertRaisesRegex(ValueError, "predates binding"):
            self.read()

    def test_receipt_published_in_binding_commit_is_denied(self):
        self.write()
        self.inject(policy.receipt_path(self.dispatch_id, self.first, "claim"), {})
        with self.assertRaisesRegex(ValueError, "predates binding"):
            self.read()

    def test_valid_receipts_are_redacted_and_still_not_send_authority(self):
        self.write()
        self.api._advance_out_of_band()
        parent = json.loads(
            self.api.snapshots[self.api.head][owners.policy.path(self.parent.parent_id)]
        )
        item = self.dispatch["children"][0]
        record = self.record()
        digest = policy.binding_digest(
            record, parent, self.dispatch,
            expected_origin_head_sha=record["binding_origin_head_sha"],
        )
        common = {
            "schema_version": 2, "dispatch_id": self.dispatch_id,
            "child_request_id": self.first,
            "spawn_transaction_id": item["spawn"]["transaction_id"],
            "binding_digest": digest,
        }
        claim = {
            **common, "kind": "browser_child_claim_v2",
            "parent_id": parent["id"], "parent_owner_id": self.owner_id,
            "fence_epoch": 1, "effect_id": record["effect_id"],
            "driver_session_id": record["driver_session_id"],
        }
        ack = {
            **common, "kind": "browser_spawn_ack_v2",
            "bootstrap_digest": item["spawn"]["bootstrap_digest"],
            "child_conversation_url": CHILD_URL,
        }
        terminal = {
            **common, "kind": "browser_terminal_result_v2",
            "child_conversation_url": CHILD_URL,
            "assistant_identity": "synthetic-assistant",
            "assistant_text": "THIS SECRET MUST NOT APPEAR IN PROJECTION",
        }
        for kind, value in (("claim", claim), ("ack", ack), ("result", terminal)):
            self.inject(policy.receipt_path(self.dispatch_id, self.first, kind), value)
        projected = self.read()
        self.assertEqual(projected["status"], "completed_evidence_only")
        self.assertIs(projected["browser_send_authorized"], False)
        self.assertNotIn("THIS SECRET", str(projected))
        self.assertNotIn("assistant_text", str(projected))
        # A later commit modifying an earlier immutable receipt is not evidence.
        self.api._advance_out_of_band()
        tampered = {**ack, "child_conversation_url": "https://chatgpt.com/c/forged"}
        self.inject(policy.receipt_path(self.dispatch_id, self.first, "ack"), tampered)
        with self.assertRaisesRegex(ValueError, "changed in pinned history"):
            self.read()


class PinnedThreeChildCollisionTests(unittest.TestCase):
    """Two non-selected siblings may not collide behind a valid selected child."""

    def setUp(self):
        self.api = LineageGitDataAPI()
        workflows = json.dumps({"schema_version": 1, "workflow_ids": ["workflow-001"]})
        self.api.snapshots[self.api.head][catalog.WORKFLOWS_PATH] = workflows
        self.api.trees["b" * 40][catalog.WORKFLOWS_PATH] = workflows
        request = operator_request()
        request["children"].append({
            "request_id": "child-third",
            "node_id": "third",
            "role": "integration",
            "summary": "Check another synthetic child.",
            "paths": ["chat_bridge"],
        })
        admitted = admitted_children() + [
            child_request(
                "child-third", "third", "integration",
                "Check another synthetic child.", ["chat_bridge"],
            )
        ]
        staged = live.stage_private_dispatch(
            request, admitted, enabled=True,
            writer_authorized=True, api=self.api,
        )
        self.dispatch_id = staged.dispatch_id
        self.owner_id = "1" * 32
        self.parent = owners.acquire_parent_candidate(
            self.dispatch_id, self.owner_id,
            enabled=True, writer_authorized=True, api=self.api,
        )
        self.dispatch = json.loads(
            self.api.snapshots[self.api.head][catalog.dispatch_path(self.dispatch_id)]
        )

    def publish(self, child, driver):
        return writer.publish_synthetic_browser_binding(
            self.dispatch_id, self.owner_id, 1, child,
            driver_session_id=driver, enabled=True,
            writer_authorized=True, api=self.api,
        )

    def assert_read_rejects(self, fragment):
        with self.assertRaisesRegex(ValueError, fragment):
            reader.read_synthetic_pinned_browser_binding(
                self.dispatch_id, "child-research",
                enabled=True, api=self.api,
            )

    def test_collision_between_two_non_selected_driver_sessions(self):
        self.publish("child-research", "b" * 32)
        self.publish("child-verify", "c" * 32)
        snapshot = owners._snapshot(self.api, self.dispatch_id)
        assert snapshot.parent is not None
        # Test-only injection of a malicious competing writer, bypassing
        # the public writer's sibling uniqueness preflight.
        forged = policy.propose_binding(
            snapshot.parent, snapshot.dispatch, "child-third",
            driver_session_id="c" * 32, source_head_sha=snapshot.head,
        )
        writer._commit(self.api, snapshot, forged)
        self.assert_read_rejects("driver session reused")

    def test_collision_between_two_non_selected_child_urls(self):
        self.publish("child-research", "b" * 32)
        self.publish("child-verify", "c" * 32)
        self.publish("child-third", "d" * 32)
        self.api._advance_out_of_band()
        url = "https://chatgpt.com/c/same-synthetic-child"
        for child_id in ("child-verify", "child-third"):
            record_path = policy.binding_path(self.parent.parent_id, 1, child_id)
            binding_record = json.loads(
                self.api.snapshots[self.api.head][record_path]
            )
            item = next(
                child for child in self.dispatch["children"]
                if child["request_id"] == child_id
            )
            owner = json.loads(
                self.api.snapshots[self.api.head][
                    owners.policy.path(self.parent.parent_id)
                ]
            )
            ack = {
                "schema_version": 2,
                "kind": "browser_spawn_ack_v2",
                "dispatch_id": self.dispatch_id,
                "child_request_id": child_id,
                "spawn_transaction_id": item["spawn"]["transaction_id"],
                "binding_digest": policy.binding_digest(
                    binding_record, owner, self.dispatch,
                    expected_origin_head_sha=binding_record["binding_origin_head_sha"],
                ),
                "bootstrap_digest": item["spawn"]["bootstrap_digest"],
                "child_conversation_url": url,
            }
            receipt_path = policy.receipt_path(self.dispatch_id, child_id, "ack")
            data = json.dumps(ack)
            self.api.snapshots[self.api.head][receipt_path] = data
            self.api.trees[self.api.commit_trees[self.api.head]][receipt_path] = data
        self.assert_read_rejects("child URL reused")


if __name__ == "__main__":
    unittest.main()
