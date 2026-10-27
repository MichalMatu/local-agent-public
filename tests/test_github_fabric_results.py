"""Synthetic-only terminal projection contract, causal CAS and recovery tests."""

from __future__ import annotations

import copy
import json
import unittest

from local_agent.conversation import github_fabric_claims_github as claims_writer
from local_agent.conversation import github_fabric_github as git
from local_agent.conversation import github_fabric_results as results
from local_agent.conversation import github_fabric_results_github as writer
from tests.test_github_fabric_claims import AtomicGitDataAPI
from tests.test_github_fabric_dispatch import admitted_children, operator_request


class GithubFabricSyntheticResultTests(unittest.TestCase):
    def setUp(self):
        self.api = AtomicGitDataAPI()
        self.operator = operator_request()
        self.children = admitted_children()
        self.result = results.public_synthetic_terminal_result(self.operator)
        self.projection = results.build_projection(
            self.operator, self.children, self.result
        )

    def predecessors(self):
        git.publish_synthetic_fixture(
            self.operator, self.children, enabled=True, api=self.api
        )
        claims_writer.publish_synthetic_claims(
            self.operator, self.children, enabled=True, api=self.api
        )

    def publish(self, **kwargs):
        defaults = {"enabled": True, "api": self.api}
        defaults.update(kwargs)
        return writer.publish_synthetic_result_fixture(
            self.operator, self.children, self.result, **defaults
        )

    def test_unattested_projection_contains_no_raw_bootstrap_or_private_summary(self):
        self.assertEqual(self.projection["kind"], results.SOURCE_KIND)
        self.assertEqual(self.projection["ack_state"], "not_attested")
        self.assertEqual(self.projection["execution_state"], "not_attested")
        self.assertEqual(self.projection["operator_result_state"], "completed")
        self.assertEqual(len(self.projection["children"]), 2)
        self.assertNotIn("bootstrap_text", json.dumps(self.projection))
        self.assertNotIn("PUBLIC SYNTHETIC RESEARCH RESULT", json.dumps(self.projection))
        self.assertNotIn("send_authorized", self.projection)
        self.assertEqual(self.projection["id"], results.result_id(self.projection["dispatch_id"]))

    def test_default_disabled_and_changed_fixture_denied_before_any_io(self):
        with self.assertRaises(PermissionError):
            writer.publish_synthetic_result_fixture(
                self.operator, self.children, self.result, api=self.api
            )
        changed = copy.deepcopy(self.result)
        changed["children"][0]["summary"] = "Private value in result"
        with self.assertRaises(PermissionError):
            writer.publish_synthetic_result_fixture(
                self.operator, self.children, changed, enabled=True, api=self.api
            )
        changed_child = copy.deepcopy(self.children)
        changed_child[0]["scope"]["summary"] = "PRIVATE OPERATOR REQUEST"
        with self.assertRaises(PermissionError):
            writer.publish_synthetic_result_fixture(
                self.operator, changed_child, self.result, enabled=True, api=self.api
            )
        self.assertEqual(self.api.operations, [])

    def test_without_dispatch_or_claims_no_result_commit_occurs(self):
        with self.assertRaisesRegex(ValueError, "dispatch index is missing"):
            self.publish()
        self.assertEqual(len(self.api.commits), 0)
        git.publish_synthetic_fixture(
            self.operator, self.children, enabled=True, api=self.api
        )
        with self.assertRaisesRegex(ValueError, "claim index is missing"):
            self.publish()
        self.assertEqual(len(self.api.commits), 2)

    def test_one_atomic_result_record_and_index_then_exact_restart_replay(self):
        self.predecessors()
        result = self.publish()
        self.assertEqual(result.status, "converged")
        self.assertEqual(result.completed_commits, 1)
        self.assertEqual(len(self.api.commits), 4)
        final = self.api.snapshots[self.api.head]
        self.assertEqual(
            json.loads(final[results.result_path(result.result_id)]), self.projection
        )
        self.assertEqual(
            json.loads(final[results.INDEX_PATH])["result_ids"], [result.result_id]
        )
        entries = [
            entry[2]["tree"] for entry in self.api.operations
            if entry[0] == "POST" and entry[1] == "/git/trees"
        ]
        self.assertEqual(len(entries), 4)
        self.assertEqual(len(entries[-1]), 2)
        self.assertEqual(
            {entry["path"] for entry in entries[-1]},
            {results.result_path(result.result_id), results.INDEX_PATH}
        )
        head = self.api.head
        again = self.publish()
        self.assertEqual(again.status, "replay")
        self.assertEqual(again.completed_commits, 0)
        self.assertEqual(again.head_sha, head)
        self.assertEqual(len(self.api.commits), 4)

    def test_lost_ref_ack_recovered_without_new_result(self):
        self.predecessors()
        self.api.fail_after_ref = True
        result = self.publish()
        self.assertEqual(result.status, "converged")
        self.assertEqual(result.completed_commits, 0)
        self.assertEqual(len(self.api.commits), 4)
        self.assertEqual(self.publish().status, "replay")
        self.assertEqual(len(self.api.commits), 4)

    def test_non_fast_forward_resnapshots_origin_then_converges(self):
        self.predecessors()
        self.api.reject_next_ref = True
        result = self.publish()
        self.assertEqual(result.status, "converged")
        self.assertEqual(result.completed_commits, 1)
        self.assertEqual(len(self.api.commits), 5)
        patches = [event for event in self.api.operations
                   if event[0] == "PATCH" and event[1] == git.GITHUB_REF_UPDATE_PATH]
        self.assertTrue(all(event[2].get("force") is False for event in patches))

    def test_remote_same_id_different_projection_denied_without_rewrite(self):
        self.predecessors()
        self.publish()
        path = results.result_path(self.projection["id"])
        mutated = copy.deepcopy(self.projection)
        mutated["children"][0]["evidence_digest"] = "sha256:" + "c" * 64
        self.api.snapshots[self.api.head][path] = json.dumps(mutated)
        with self.assertRaisesRegex(ValueError, "same-ID evidence conflict"):
            self.publish()
        self.assertEqual(len(self.api.commits), 4)

    def test_dangling_or_orphan_result_index_fails_closed(self):
        self.predecessors()
        self.publish()
        path = results.result_path(self.projection["id"])
        self.api.snapshots[self.api.head].pop(path)
        with self.assertRaisesRegex(ValueError, "index references missing record"):
            self.publish()
        self.assertEqual(len(self.api.commits), 4)
        self.api.snapshots[self.api.head][path] = json.dumps(self.projection)
        self.api.snapshots[self.api.head].pop(results.INDEX_PATH)
        with self.assertRaisesRegex(ValueError, "Unindexed synthetic result record"):
            self.publish()
        self.assertEqual(len(self.api.commits), 4)

    def test_unavailable_or_corrupt_source_fails_closed(self):
        self.predecessors()
        self.api.deny_next_request = True
        with self.assertRaises(git.GithubFabricHTTPError) as error:
            self.publish()
        self.assertEqual(error.exception.status, 403)
        self.api.corrupt_path = results.INDEX_PATH
        with self.assertRaises(ValueError):
            self.publish()
        self.assertEqual(len(self.api.commits), 3)

    def test_schema_size_id_and_phase_downgrades_rejected(self):
        with self.assertRaises(ValueError):
            results.result_path("../index.json")
        with self.assertRaises(ValueError):
            results.result_id("fabric-wrong")
        with self.assertRaises(ValueError):
            results.validate_index({"schema_version": 1, "result_ids": [
                "result-" + "a" * 32, "result-" + "a" * 32
            ]})
        forged = copy.deepcopy(self.projection)
        forged["ack_state"] = "received_from_browser"
        with self.assertRaisesRegex(ValueError, "projection invalid"):
            results.validate_projection(forged)
        forged = copy.deepcopy(self.projection)
        forged["children"][0]["operator_child_state"] = "waiting"
        with self.assertRaisesRegex(ValueError, "child projection invalid"):
            results.validate_projection(forged)
        self.assertNotEqual(
            results.result_id("fabric-" + "0" * 32),
            results.result_id("fabric-" + "f" * 32),
        )


if __name__ == "__main__":
    unittest.main()
