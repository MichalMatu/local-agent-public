"""Commit-pinned, read-only private synthetic workflow recovery tests."""

from __future__ import annotations

import copy
import json
import unittest
from dataclasses import asdict

from local_agent.conversation import github_fabric_github as git
from local_agent.conversation import github_fabric_private_github as private_git
from local_agent.conversation import github_fabric_private_publication as scope
from local_agent.conversation import github_fabric_private_recovery as reader
from tests.test_github_fabric_dispatch import admitted_children, operator_request
from tests.test_github_fabric_private_github import PrivateGitDataAPI


class PrivateSyntheticColdRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.api = PrivateGitDataAPI()
        self.request = operator_request()
        self.children = admitted_children()

    def publish(self):
        return private_git.publish_private_synthetic_fixture(
            self.request, self.children, enabled=True, api=self.api
        )

    def recover(self, **updates):
        kwargs = {"enabled": True, "api": self.api}
        kwargs.update(updates)
        return reader.recover_private_synthetic_dispatch(
            self.request, self.children, **kwargs
        )

    def test_default_disabled_and_private_payload_rejected_before_io(self):
        with self.assertRaises(PermissionError):
            reader.recover_private_synthetic_dispatch(
                self.request, self.children, api=self.api
            )
        self.children[0]["scope"]["summary"] = "PRIVATE CLIENT INPUT"
        with self.assertRaises(PermissionError):
            self.recover()
        self.assertEqual(self.api.operations, [])

    def test_cold_restart_reads_same_private_origin_without_mutation(self):
        published = self.publish()
        before = len(self.api.operations)
        first = self.recover()
        self.assertEqual(first.source_kind, reader.SOURCE_KIND)
        self.assertEqual(first.source_head_sha, published.head_sha)
        self.assertEqual(first.project_id, "local-agent")
        self.assertEqual(first.workflow_id, "workflow-001")
        self.assertEqual(first.operator_request_id, self.request["id"])
        self.assertEqual(len(first.children), 2)
        self.assertTrue(all(c.execution_state == reader.EXECUTION_STATE for c in first.children))
        self.assertNotIn("bootstrap_text", json.dumps(asdict(first)))
        self.assertNotIn("send_authorized", json.dumps(asdict(first)))
        self.assertEqual([x[0] for x in self.api.operations[before:]], ["GET"] * 6)
        # No local durable cache is used; a fresh call reconstructs from GitHub.
        repeated = self.recover()
        self.assertEqual(repeated, first)
        self.assertEqual(published.head_sha, self.api.head)
        self.assertEqual(len(self.api.commits), 3)

    def test_empty_workflow_refuses_recovery_with_no_side_effect(self):
        with self.assertRaisesRegex(ValueError, "not fully indexed"):
            self.recover()
        self.assertEqual(len(self.api.commits), 0)

    def test_orphan_dispatch_and_partial_index_are_not_complete(self):
        self.publish()
        self.api.snapshots[self.api.head][scope.WORKFLOWS_PATH] = json.dumps({
            "schema_version": 1, "workflow_ids": [],
        })
        with self.assertRaisesRegex(ValueError, "not fully indexed"):
            self.recover()
        self.assertEqual(len(self.api.commits), 3)

    def test_dangling_dispatch_index_fails_closed(self):
        self.publish()
        index = json.loads(self.api.snapshots[self.api.head][scope.INDEX_PATH])
        path = scope.dispatch_path(index["dispatch_ids"][0])
        self.api.snapshots[self.api.head].pop(path)
        with self.assertRaisesRegex(ValueError, "indexed dispatch record missing"):
            self.recover()
        self.assertEqual(len(self.api.commits), 3)

    def test_mutated_existing_dispatch_conflicts(self):
        self.publish()
        paths = list(self.api.snapshots[self.api.head])
        path = next(x for x in paths if x.startswith(scope.DISPATCH_ROOT)
                    and x.endswith(".json") and x != scope.INDEX_PATH)
        changed = copy.deepcopy(json.loads(self.api.snapshots[self.api.head][path]))
        changed["children"][0]["spawn"]["bootstrap_text"] += "\nTAMPERED"
        self.api.snapshots[self.api.head][path] = json.dumps(changed)
        with self.assertRaisesRegex(ValueError, "same-id dispatch conflict"):
            self.recover()

    def test_permission_denied_and_malformed_source_rejected(self):
        self.publish()
        self.api.deny_next_request = True
        with self.assertRaises(git.GithubFabricHTTPError) as problem:
            self.recover()
        self.assertEqual(problem.exception.status, 403)
        self.api.corrupt_path = scope.INDEX_PATH
        with self.assertRaises(ValueError):
            self.recover()

    def test_private_transport_not_public_fallback(self):
        self.publish()
        calls = len(self.api.operations)
        self.recover()
        paths = [x[1] for x in self.api.operations[calls:] if x[0] == "GET"]
        self.assertTrue(all(
            x == private_git.REF_PATH
            or x.startswith("/git/commits/")
            or x.startswith("/contents/projects/")
            for x in paths
        ))
        self.assertNotIn(git.GITHUB_REF_PATH, paths)


if __name__ == "__main__":
    unittest.main()
