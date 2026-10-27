"""Atomic semantic-node claim plans and isolated GitHub CAS recovery."""

from __future__ import annotations

import copy
import json
import unittest

from local_agent.conversation import github_fabric_claims as claims
from local_agent.conversation import github_fabric_claims_github as writer
from local_agent.conversation import github_fabric_github as git
from tests.test_github_fabric_dispatch import admitted_children, operator_request
from tests.test_github_fabric_github import FakeGitDataAPI


class AtomicGitDataAPI(FakeGitDataAPI):
    """Same authenticated Git Data fake, with multiple atomic tree entries."""

    def request(self, method, path, body=None):
        if method != "POST" or path != "/git/trees":
            return super().request(method, path, body)
        self.operations.append((method, path, copy.deepcopy(body)))
        entries = body["tree"]
        if not 1 <= len(entries) <= claims.MAX_CLAIMS + 1:
            raise AssertionError("atomic claim update has wrong entry count")
        if len({entry["path"] for entry in entries}) != len(entries):
            raise AssertionError("duplicate tree path")
        contents = copy.deepcopy(self.trees[body["base_tree"]])
        for entry in entries:
            if entry["mode"] != "100644" or entry["type"] != "blob":
                raise AssertionError("unsafe atomic entry")
            contents[entry["path"]] = self.blobs[entry["sha"]]
        sha = self.new_sha()
        self.trees[sha] = contents
        return {"sha": sha}


class SyntheticSemanticClaimTests(unittest.TestCase):
    def setUp(self):
        self.operator = operator_request()
        self.children = admitted_children()
        self.claims = claims.build_claims(self.operator, self.children)
        self.api = AtomicGitDataAPI()

    def plan(self, **updates):
        defaults = {
            "existing_index": None,
            "existing_records": {claim["id"]: None for claim in self.claims},
            "expected_head_sha": "a" * 40,
            "enabled": True,
            "writer_authorized": True,
        }
        defaults.update(updates)
        return claims.preflight_synthetic_claims(self.operator, self.children, **defaults)

    def publish(self, **updates):
        kwargs = {"api": self.api, "enabled": True}
        kwargs.update(updates)
        return writer.publish_synthetic_claims(self.operator, self.children, **kwargs)

    def test_semantic_identity_ignores_independent_transaction_algorithm(self):
        self.assertEqual(len(self.claims), 2)
        self.assertEqual(self.claims, claims.build_claims(
            copy.deepcopy(self.operator), list(reversed(self.children))
        ))
        for claim in self.claims:
            self.assertEqual(claim["id"], claims.claim_id(
                claim["workflow_id"], claim["workflow_node_id"]
            ))
            self.assertEqual(claim["mode"], "synthetic_observation_only")
            self.assertEqual(claim["phase"], "admission_preview")
            self.assertNotIn("bootstrap_text", claim)
            self.assertNotIn("token", claim)
        self.assertNotEqual(
            claims.claim_id("workflow-001", "research"),
            claims.claim_id("workflow-002", "research")
        )

    def test_default_disabled_and_private_source_rejected_before_io(self):
        with self.assertRaises(PermissionError):
            self.plan(enabled=False)
        with self.assertRaises(PermissionError):
            self.plan(writer_authorized=False)
        with self.assertRaises(PermissionError):
            writer.publish_synthetic_claims(self.operator, self.children, api=self.api)
        self.children[0]["scope"]["summary"] = "PRIVATE USER PROJECT"
        with self.assertRaises(PermissionError):
            self.publish()
        self.assertEqual(self.api.operations, [])

    def test_claim_and_index_are_one_atomic_git_commit_then_exact_replay(self):
        proposed = self.plan()
        self.assertEqual(proposed.operation, "commit_atomic")
        self.assertEqual(len(proposed.writes), 3)
        self.assertEqual(proposed.writes[-1][0], claims.INDEX_PATH)
        result = self.publish()
        self.assertEqual(result.status, "converged")
        self.assertEqual(result.completed_commits, 1)
        self.assertEqual(len(self.api.commits), 1)
        commit = next(iter(self.api.commits.values()))
        self.assertEqual(commit["parents"], ["a" * 40])
        tree_entries = [o[2]["tree"] for o in self.api.operations
                        if o[0] == "POST" and o[1] == "/git/trees"]
        self.assertEqual(len(tree_entries), 1)
        self.assertEqual(len(tree_entries[0]), 3)
        remote = self.api.snapshots[self.api.head]
        index = json.loads(remote[claims.INDEX_PATH])
        self.assertEqual(index["claim_ids"], [c["id"] for c in self.claims])
        for claim in self.claims:
            self.assertEqual(json.loads(remote[claims.claim_path(claim["id"])]), claim)
        after = self.publish()
        self.assertEqual(after.status, "replay")
        self.assertEqual(after.completed_commits, 0)
        self.assertEqual(len(self.api.commits), 1)

    def test_same_node_different_payload_is_permanent_conflict(self):
        result = self.publish()
        original_id = result.claim_ids[0]
        path = claims.claim_path(original_id)
        changed = copy.deepcopy(self.claims[0])
        changed["child_request_digest"] = "sha256:" + "f" * 64
        self.api.snapshots[self.api.head][path] = json.dumps(changed)
        with self.assertRaisesRegex(ValueError, "same-semantic-node claim conflict"):
            self.publish()
        self.assertEqual(len(self.api.commits), 1)

    def test_lost_commit_ack_converges_without_double_commit(self):
        self.api.fail_after_ref = True
        result = self.publish()
        self.assertEqual(result.status, "converged")
        self.assertEqual(result.completed_commits, 0)
        self.assertEqual(len(self.api.commits), 1)
        self.assertEqual(self.publish().status, "replay")
        self.assertEqual(len(self.api.commits), 1)

    def test_parallel_ref_competition_reloads_and_replans(self):
        self.api.reject_next_ref = True
        result = self.publish()
        self.assertEqual(result.status, "converged")
        self.assertEqual(result.completed_commits, 1)
        self.assertGreaterEqual(len(self.api.commits), 2)
        updates = [op for op in self.api.operations
                   if op[0] == "PATCH" and op[1] == git.GITHUB_REF_UPDATE_PATH]
        self.assertGreaterEqual(len(updates), 2)
        self.assertTrue(all(op[2]["force"] is False for op in updates))

    def test_index_missing_record_or_orphan_never_rewritten(self):
        self.publish()
        record = self.claims[0]
        path = claims.claim_path(record["id"])
        self.api.snapshots[self.api.head].pop(path)
        with self.assertRaisesRegex(ValueError, "missing claim"):
            self.publish()
        self.assertEqual(len(self.api.commits), 1)
        self.api.snapshots[self.api.head][path] = json.dumps(record)
        self.api.snapshots[self.api.head].pop(claims.INDEX_PATH)
        with self.assertRaisesRegex(ValueError, "Unindexed Fabric claim"):
            self.publish()
        self.assertEqual(len(self.api.commits), 1)

    def test_invalid_index_denied_access_and_corrupt_remote_fail_closed(self):
        self.api.deny_next_request = True
        with self.assertRaises(git.GithubFabricHTTPError) as result:
            self.publish()
        self.assertEqual(result.exception.status, 403)
        self.assertEqual(len(self.api.commits), 0)
        self.api.corrupt_path = claims.INDEX_PATH
        with self.assertRaises(ValueError):
            self.publish()
        self.assertEqual(len(self.api.commits), 0)

    def test_reject_index_overflow_invalid_sha_paths_and_forged_records(self):
        with self.assertRaises(ValueError):
            self.plan(expected_head_sha="main")
        with self.assertRaises(ValueError):
            claims.claim_path("../escape")
        with self.assertRaises(ValueError):
            claims.validate_index({"schema_version": 1, "claim_ids": ["claim-" + "a"*32]*2})
        with self.assertRaises(ValueError):
            claims.validate_index({"schema_version": True, "claim_ids": []})
        with self.assertRaises(ValueError):
            claims.validate_index({"schema_version": 1, "claim_ids": ["claim-" + "a"*32]*5})
        changed = copy.deepcopy(self.claims[0])
        changed["id"] = "claim-" + "f"*32
        with self.assertRaisesRegex(ValueError, "identity mismatch"):
            claims.validate_claim(changed)

    def test_ref_conflicts_exhaust_retry_budget(self):
        delegate = self.api
        class AlwaysConflict:
            def request(self, method, path, body=None):
                if method == "PATCH":
                    raise git.GithubFabricHTTPError(422)
                return delegate.request(method, path, body)
        with self.assertRaisesRegex(RuntimeError, "did not converge"):
            self.publish(api=AlwaysConflict())
        self.assertEqual(delegate.head, "a" * 40)


if __name__ == "__main__":
    unittest.main()
