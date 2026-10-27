"""Private scoped GitHub Fabric publication: exact synthetic source only."""

from __future__ import annotations

import copy
import json
import unittest

from local_agent.conversation import github_fabric_github as public_git
from local_agent.conversation import github_fabric_private_github as writer
from local_agent.conversation import github_fabric_private_publication as scope
from tests.test_github_fabric_dispatch import admitted_children, operator_request
from tests.test_github_fabric_github import FakeGitDataAPI


class PrivateGitDataAPI(FakeGitDataAPI):
    def __init__(self):
        super().__init__()
        seed = {
            scope.CATALOG_PATH: json.dumps({
                "schema_version": 1,
                "project_ids": ['example-device', 'example-project', 'local-agent'],
            }),
            scope.WORKFLOWS_PATH: json.dumps({
                "schema_version": 1, "workflow_ids": []
            }),
        }
        self.snapshots[self.head] = copy.deepcopy(seed)
        self.trees["b" * 40] = copy.deepcopy(seed)

    def request(self, method, path, body=None):
        if method == "GET" and path == writer.REF_PATH:
            self.operations.append((method, path, None))
            return {"ref": "refs/heads/fabric-data", "object": {
                "type": "commit", "sha": self.head,
            }}
        if method == "PATCH" and path == writer.REF_UPDATE_PATH:
            return super().request(method, public_git.GITHUB_REF_UPDATE_PATH, body)
        return super().request(method, path, body)


class PrivateSyntheticWriterTests(unittest.TestCase):
    def setUp(self):
        self.api = PrivateGitDataAPI()
        self.operator = operator_request()
        self.children = admitted_children()

    def publish(self, **kwargs):
        settings = {"api": self.api, "enabled": True}
        settings.update(kwargs)
        return writer.publish_private_synthetic_fixture(
            self.operator, self.children, **settings
        )

    def test_scope_bound_paths_and_catalog_validation(self):
        self.assertEqual(scope.CATALOG_PATH, "projects/index.json")
        self.assertEqual(scope.WORKFLOWS_PATH, "projects/local-agent/workflows/index.json")
        self.assertTrue(scope.INDEX_PATH.startswith(
            "projects/local-agent/workflows/workflow-001/dispatches/"
        ))
        with self.assertRaises(ValueError):
            scope.dispatch_path("../../not-a-dispatch")
        with self.assertRaises(ValueError):
            scope.validate_projects({"schema_version": 1, "project_ids": ["local-agent"] * 2})
        with self.assertRaises(ValueError):
            scope.validate_workflows({"schema_version": 1, "workflow_ids": ["../escape"]})
        with self.assertRaises(ValueError):
            scope.validate_dispatch_index({"schema_version": True, "dispatch_ids": []})

    def test_disabled_and_unapproved_input_deny_before_remote_io(self):
        with self.assertRaises(PermissionError):
            writer.publish_private_synthetic_fixture(self.operator, self.children, api=self.api)
        self.children[0]["scope"]["summary"] = "PRIVATE CLIENT SCOPE"
        with self.assertRaises(PermissionError):
            self.publish()
        self.assertEqual(self.api.operations, [])
        self.children = admitted_children()
        self.operator["workflow_id"] = "other-workflow"
        with self.assertRaises(PermissionError):
            self.publish()
        self.assertEqual(self.api.operations, [])

    def test_publish_record_first_index_second_workflow_index_last(self):
        result = self.publish()
        self.assertEqual(result.status, "converged")
        self.assertEqual(result.applied_steps, (
            "create_record", "publish_dispatch_index", "publish_workflow_index"
        ))
        self.assertEqual(len(self.api.commits), 3)
        final = self.api.snapshots[self.api.head]
        self.assertEqual(
            json.loads(final[scope.WORKFLOWS_PATH])["workflow_ids"], ["workflow-001"]
        )
        self.assertEqual(len(json.loads(final[scope.INDEX_PATH])["dispatch_ids"]), 1)
        ref_writes = [event for event in self.api.operations if event[0] == "PATCH"]
        self.assertEqual(len(ref_writes), 3)
        self.assertTrue(all(event[2]["force"] is False for event in ref_writes))
        first = list(self.api.commits.values())[0]
        second = list(self.api.commits.values())[1]
        third = list(self.api.commits.values())[2]
        self.assertEqual(first["parents"], ["a" * 40])
        self.assertEqual(
            self.api.trees[first["tree"]].keys() & {scope.INDEX_PATH}, set()
        )
        self.assertIn(scope.INDEX_PATH, self.api.trees[second["tree"]])
        self.assertEqual(
            json.loads(self.api.trees[third["tree"]][scope.WORKFLOWS_PATH])["workflow_ids"],
            ["workflow-001"]
        )
        replay = self.publish()
        self.assertEqual(replay.status, "replay")
        self.assertEqual(replay.applied_steps, ())
        self.assertEqual(len(self.api.commits), 3)

    def test_partial_publication_resumes_from_exact_origin(self):
        self.api.fail_after_ref_number = 1
        result = self.publish()
        self.assertEqual(result.status, "converged")
        self.assertEqual(len(self.api.commits), 3)
        self.assertEqual(result.applied_steps, (
            "publish_dispatch_index", "publish_workflow_index"
        ))
        self.assertEqual(self.publish().status, "replay")
        self.assertEqual(len(self.api.commits), 3)

    def test_lost_last_ack_does_not_duplicate(self):
        self.api.fail_after_ref_number = 3
        result = self.publish()
        self.assertEqual(result.status, "converged")
        self.assertEqual(len(self.api.commits), 3)
        self.assertEqual(result.applied_steps, ("create_record", "publish_dispatch_index"))
        self.assertEqual(self.publish().status, "replay")
        self.assertEqual(len(self.api.commits), 3)

    def test_fast_forward_conflict_rechecks_origin(self):
        self.api.reject_next_ref = True
        result = self.publish()
        self.assertEqual(result.status, "converged")
        self.assertGreaterEqual(len(self.api.commits), 4)

    def test_missing_catalog_or_corrupt_existing_record_fail_closed(self):
        self.api.snapshots[self.api.head].pop(scope.CATALOG_PATH)
        with self.assertRaisesRegex(ValueError, "project or workflow index absent"):
            self.publish()
        self.assertEqual(len(self.api.commits), 0)
        self.api = PrivateGitDataAPI()
        self.publish()
        record_path = next(path for path in self.api.snapshots[self.api.head]
                           if path.startswith(scope.DISPATCH_ROOT) and path.endswith(".json")
                           and path != scope.INDEX_PATH)
        record = json.loads(self.api.snapshots[self.api.head][record_path])
        record["children"][0]["spawn"]["bootstrap_text"] += "\nCORRUPTION"
        self.api.snapshots[self.api.head][record_path] = json.dumps(record)
        with self.assertRaisesRegex(ValueError, "same-id dispatch conflict"):
            self.publish()
        self.assertEqual(len(self.api.commits), 3)

    def test_access_denied_and_malformed_ref_do_not_publish(self):
        self.api.deny_next_request = True
        with self.assertRaises(public_git.GithubFabricHTTPError) as error:
            self.publish()
        self.assertEqual(error.exception.status, 403)
        self.assertEqual(len(self.api.commits), 0)
        class InvalidRef:
            def request(self, method, path, body=None):
                return {"ref": "refs/heads/other", "object": {
                    "type": "commit", "sha": "a" * 40,
                }}
        with self.assertRaisesRegex(ValueError, "origin ref is invalid"):
            self.publish(api=InvalidRef())

    def test_repository_and_branch_are_fixed_never_public_fallback(self):
        self.assertEqual(writer.PRIVATE_REPOSITORY, "example-owner/local-agent-fabric-private")
        self.assertEqual(writer.PRIVATE_BRANCH, "fabric-data")
        self.assertEqual(writer.API_ROOT, "https://api.github.com/repos/" + writer.PRIVATE_REPOSITORY)
        for token in ("", " token", "token\nInjected: yes"):
            with self.assertRaises(PermissionError):
                writer.PrivateFabricREST(token)
        self.assertNotEqual(writer.API_ROOT, public_git.GITHUB_API_ROOT)


if __name__ == "__main__":
    unittest.main()
