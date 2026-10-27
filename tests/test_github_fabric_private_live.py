"""Private real dispatch staging: one atomic GitHub commit, no browser effects."""

from __future__ import annotations

import copy
import json
import unittest

from local_agent.conversation import github_fabric_dispatch as dispatch_contract
from local_agent.conversation import github_fabric_github as git
from local_agent.conversation import github_fabric_private_live as live
from local_agent.conversation import github_fabric_private_publication as catalog
from tests.test_github_fabric_dispatch import admitted_children, operator_request
from tests.test_github_fabric_private_github import PrivateGitDataAPI


class AtomicPrivateGitDataAPI(PrivateGitDataAPI):
    """The older Git Data fixture only accepted single-path trees."""

    def request(self, method, path, body=None):
        if method == "POST" and path == "/git/trees" and len(body["tree"]) == 2:
            self.operations.append((method, path, copy.deepcopy(body)))
            sha = self.new_sha()
            contents = copy.deepcopy(self.trees[body["base_tree"]])
            for entry in body["tree"]:
                if entry["mode"] != "100644":
                    raise AssertionError("unsafe Git blob mode")
                contents[entry["path"]] = self.blobs[entry["sha"]]
            self.trees[sha] = contents
            return {"sha": sha}
        return super().request(method, path, body)


class PrivateLivePublisherTests(unittest.TestCase):
    def setUp(self):
        self.api = AtomicPrivateGitDataAPI()
        # An operator-controlled workflow catalog already grants discovery,
        # but deliberately grants NO browser Send authority.
        workflows = json.dumps({"schema_version": 1, "workflow_ids": ["workflow-001"]})
        self.api.snapshots[self.api.head][catalog.WORKFLOWS_PATH] = workflows
        self.api.trees["b" * 40][catalog.WORKFLOWS_PATH] = workflows
        self.operator = operator_request()
        self.children = admitted_children()

    def publish(self, **kwargs):
        settings = {"enabled": True, "writer_authorized": True, "api": self.api}
        settings.update(kwargs)
        return live.stage_private_dispatch(self.operator, self.children, **settings)

    def test_default_denied_before_io(self):
        with self.assertRaisesRegex(PermissionError, "disabled"):
            live.stage_private_dispatch(self.operator, self.children, api=self.api)
        with self.assertRaisesRegex(PermissionError, "disabled"):
            self.publish(writer_authorized=False)
        self.assertFalse(self.api.operations)

    def test_real_private_source_is_not_public_synthetic_fixture(self):
        self.operator["children"][0]["summary"] = "Private real user reasoning request"
        self.children[0]["scope"]["summary"] = "Private real user reasoning request"
        result = self.publish()
        self.assertEqual(result.status, "converged")
        self.assertEqual(result.completed_commits, 1)
        expected = dispatch_contract.build_github_fabric_dispatch(self.operator, self.children)
        stored = self.api.snapshots[self.api.head]
        self.assertEqual(json.loads(stored[catalog.dispatch_path(expected["id"])]), expected)
        self.assertEqual(json.loads(stored[catalog.INDEX_PATH])["dispatch_ids"], [expected["id"]])
        self.assertNotIn("browser_ack", expected)
        self.assertEqual(self.publish().status, "replay")
        self.assertEqual(len(self.api.commits), 1)

    def test_record_and_index_same_atomic_commit(self):
        self.publish()
        self.assertEqual(len(self.api.commits), 1)
        commit = next(iter(self.api.commits.values()))
        tree = self.api.trees[commit["tree"]]
        dispatch = dispatch_contract.build_github_fabric_dispatch(self.operator, self.children)
        self.assertIn(catalog.dispatch_path(dispatch["id"]), tree)
        self.assertIn(catalog.INDEX_PATH, tree)
        writes = [op for op in self.api.operations if op[0] == "PATCH"]
        self.assertEqual(len(writes), 1)
        self.assertIs(writes[0][2]["force"], False)

    def test_lost_ack_and_ref_collision_reconcile(self):
        self.api.fail_after_ref_number = 1
        self.assertEqual(self.publish().status, "converged")
        self.assertEqual(len(self.api.commits), 1)
        self.assertEqual(self.publish().status, "replay")
        self.api = AtomicPrivateGitDataAPI()
        workflows = json.dumps({"schema_version": 1, "workflow_ids": ["workflow-001"]})
        self.api.snapshots[self.api.head][catalog.WORKFLOWS_PATH] = workflows
        self.api.trees["b" * 40][catalog.WORKFLOWS_PATH] = workflows
        self.api.reject_next_ref = True
        self.assertEqual(self.publish().status, "converged")
        self.assertEqual(self.publish().status, "replay")

    def test_corrupt_origin_and_same_id_conflicts_never_overwrite(self):
        self.api.snapshots[self.api.head][catalog.WORKFLOWS_PATH] = json.dumps({
            "schema_version": 1, "workflow_ids": []
        })
        with self.assertRaisesRegex(PermissionError, "not indexed"):
            self.publish()
        self.assertEqual(len(self.api.commits), 0)
        self.api.snapshots[self.api.head][catalog.WORKFLOWS_PATH] = json.dumps({
            "schema_version": 1, "workflow_ids": ["workflow-001"]
        })
        self.publish()
        record_path = catalog.dispatch_path(
            dispatch_contract.build_github_fabric_dispatch(self.operator, self.children)["id"]
        )
        changed = json.loads(self.api.snapshots[self.api.head][record_path])
        changed["children"][0]["spawn"]["bootstrap_text"] += "\nMUTATED"
        self.api.snapshots[self.api.head][record_path] = json.dumps(changed)
        with self.assertRaisesRegex(ValueError, "same-id dispatch conflict"):
            self.publish()
        self.assertEqual(len(self.api.commits), 1)

    def test_invalid_scope_and_orphan_fail_closed(self):
        self.operator["workflow_id"] = "unapproved-workflow"
        for child in self.children:
            child["workflow_id"] = "unapproved-workflow"
        with self.assertRaises(PermissionError):
            self.publish()
        self.assertEqual(self.api.operations, [])
        self.operator = operator_request()
        self.children = admitted_children()
        dispatch = dispatch_contract.build_github_fabric_dispatch(self.operator, self.children)
        self.api.snapshots[self.api.head][catalog.dispatch_path(dispatch["id"])] = json.dumps(dispatch)
        with self.assertRaisesRegex(ValueError, "unindexed dispatch"):
            self.publish()
        self.assertEqual(len(self.api.commits), 0)

    def test_private_github_request_index_to_dispatch_and_replay(self):
        source_id = self.operator["id"]
        index = json.dumps({"schema_version": 1, "request_ids": [source_id]})
        source = json.dumps({
            "schema_version": 1,
            "operator_request": self.operator,
            "child_requests": self.children,
        })
        self.api.snapshots[self.api.head][live.REQUEST_INDEX] = index
        self.api.trees["b" * 40][live.REQUEST_INDEX] = index
        source_path = live.REQUEST_ROOT + source_id + ".json"
        self.api.snapshots[self.api.head][source_path] = source
        self.api.trees["b" * 40][source_path] = source
        result = live.stage_private_queued_request(
            source_id, enabled=True, writer_authorized=True, api=self.api
        )
        self.assertEqual(result.status, "converged")
        self.assertEqual(len(self.api.commits), 1)
        replay = live.stage_private_queued_request(
            source_id, enabled=True, writer_authorized=True, api=self.api
        )
        self.assertEqual(replay.status, "replay")
        self.assertEqual(len(self.api.commits), 1)

        # Mutable same-ID source cannot create a second browser dispatch.
        changed = copy.deepcopy(self.children)
        changed[0]["scope"]["summary"] += " modified"
        self.api.snapshots[self.api.head][source_path] = json.dumps({
            "schema_version": 1,
            "operator_request": self.operator,
            "child_requests": changed,
        })
        with self.assertRaisesRegex(ValueError, "same-id dispatch conflict|same operator request ID"):
            live.stage_private_queued_request(
                source_id, enabled=True, writer_authorized=True, api=self.api
            )
        self.assertEqual(len(self.api.commits), 1)

    def test_private_request_unindexed_denied_before_any_write(self):
        self.api.snapshots[self.api.head][live.REQUEST_INDEX] = json.dumps({
            "schema_version": 1, "request_ids": []
        })
        with self.assertRaisesRegex(PermissionError, "not indexed"):
            live.stage_private_queued_request(
                self.operator["id"], enabled=True, writer_authorized=True, api=self.api
            )
        self.assertEqual(len(self.api.commits), 0)
        with self.assertRaises(PermissionError):
            live.stage_private_queued_request(self.operator["id"], api=self.api)
        with self.assertRaises(ValueError):
            live.stage_private_queued_request(
                "../escape", enabled=True, writer_authorized=True, api=self.api
            )

    def test_access_denied_does_not_fallback_public(self):
        self.api.deny_next_request = True
        with self.assertRaises(git.GithubFabricHTTPError) as error:
            self.publish()
        self.assertEqual(error.exception.status, 403)
        self.assertEqual(len(self.api.commits), 0)
        self.assertEqual(live.PROJECT_ID, "local-agent")
        self.assertEqual(live.WORKFLOW_ID, "workflow-001")


if __name__ == "__main__":
    unittest.main()
