"""Private multi-project GitHub catalog: pinned, read-only, fail-closed."""

from __future__ import annotations

import json
import unittest

from local_agent.conversation import github_fabric_github as git
from local_agent.conversation import github_fabric_private_catalog as catalog
from local_agent.conversation import github_fabric_private_github as private_api
from local_agent.conversation import github_fabric_private_publication as schema
from tests.test_github_fabric_private_github import PrivateGitDataAPI


class PrivateFabricProjectCatalogTests(unittest.TestCase):
    def setUp(self) -> None:
        self.api = PrivateGitDataAPI()
        self.projects = sorted(["example-project", "local-agent", "example-device"])
        for project in ("example-project", "example-device"):
            path = f"projects/{project}/workflows/index.json"
            value = json.dumps({"schema_version": 1, "workflow_ids": []})
            self.api.snapshots[self.api.head][path] = value
            self.api.trees["b" * 40][path] = value

    def observe(self, **updates):
        options = {"enabled": True, "api": self.api}
        options.update(updates)
        return catalog.read_private_fabric_project_catalog(**options)

    def test_default_disabled_does_no_remote_io(self):
        with self.assertRaises(PermissionError):
            catalog.read_private_fabric_project_catalog(api=self.api)
        self.assertEqual(self.api.operations, [])
        with self.assertRaises(PermissionError):
            catalog.read_private_fabric_project_catalog(enabled=False)
        self.assertEqual(self.api.operations, [])

    def test_all_three_projects_read_at_same_sha_with_no_dispatch_access(self):
        result = self.observe()
        self.assertEqual(result.source_kind, catalog.SOURCE_KIND)
        self.assertEqual(result.source_head_sha, self.api.head)
        self.assertEqual([p.project_id for p in result.projects], self.projects)
        self.assertTrue(all(p.workflow_ids == () for p in result.projects))
        requests = self.api.operations
        self.assertEqual(len(requests), 5)
        self.assertEqual([method for method, _path, _body in requests], ["GET"] * 5)
        self.assertTrue(all(
            path == private_api.REF_PATH
            or path.startswith("/contents/projects/")
            for _method, path, _body in requests
        ))
        self.assertNotIn("bootstrap_text", repr(result))
        self.assertNotIn("token", repr(result))
        self.assertEqual(self.api.commits, {})

    def test_index_update_becomes_recoverable_without_conversation_tab(self):
        path = schema.WORKFLOWS_PATH
        self.api.snapshots[self.api.head][path] = json.dumps({
            "schema_version": 1, "workflow_ids": ["workflow-001"],
        })
        observed = self.observe()
        local_agent = next(p for p in observed.projects if p.project_id == "local-agent")
        self.assertEqual(local_agent.workflow_ids, ("workflow-001",))
        self.assertEqual(observed, self.observe())
        self.assertEqual(self.api.commits, {})

    def test_missing_root_or_indexed_project_fails_closed(self):
        del self.api.snapshots[self.api.head][schema.CATALOG_PATH]
        with self.assertRaisesRegex(ValueError, "catalog is missing"):
            self.observe()
        self.api = PrivateGitDataAPI()
        self.api.snapshots[self.api.head][schema.CATALOG_PATH] = json.dumps({
            "schema_version": 1, "project_ids": ["example-project", "local-agent"],
        })
        with self.assertRaisesRegex(ValueError, "workflow catalog is missing"):
            self.observe()
        self.assertEqual(self.api.commits, {})

    def test_malformed_index_and_path_escape_rejected_before_derived_lookup(self):
        self.api.snapshots[self.api.head][schema.CATALOG_PATH] = json.dumps({
            "schema_version": 1,
            "project_ids": ["../escape", "example-project", "local-agent"],
        })
        with self.assertRaises(ValueError):
            self.observe()
        self.assertEqual(len(self.api.operations), 2)
        self.assertTrue(all(
            not path.startswith("/contents/../") for _, path, _ in self.api.operations
        ))
        self.api = PrivateGitDataAPI()
        self.api.corrupt_path = schema.CATALOG_PATH
        with self.assertRaises(ValueError):
            self.observe()

    def test_stale_ref_and_denied_api_fail_closed(self):
        class BadRef:
            def request(self, method, path, body=None):
                return {
                    "ref": "refs/heads/untrusted",
                    "object": {"type": "commit", "sha": "a" * 40}
                }

        with self.assertRaisesRegex(ValueError, "origin ref invalid"):
            self.observe(api=BadRef())
        self.api.deny_next_request = True
        with self.assertRaises(git.GithubFabricHTTPError) as error:
            self.observe()
        self.assertEqual(error.exception.status, 403)
        self.assertEqual(self.api.commits, {})

    def test_snapshot_uses_original_commit_despite_concurrent_ref_update(self):
        api = self.api

        class ConcurrentWriter:
            def request(self, method, path, body=None):
                value = api.request(method, path, body)
                if method == "GET" and path == private_api.REF_PATH:
                    api._advance_out_of_band()
                    api.snapshots[api.head][schema.CATALOG_PATH] = json.dumps({
                        "schema_version": 1, "project_ids": [],
                    })
                return value

        initial = self.api.head
        observed = self.observe(api=ConcurrentWriter())
        self.assertEqual(observed.source_head_sha, initial)
        self.assertEqual([p.project_id for p in observed.projects], self.projects)
        self.assertNotEqual(self.api.head, initial)
        self.assertEqual(self.api.commits, {})

    def test_sorted_bounded_catalogs_and_no_inferred_workflows(self):
        self.api.snapshots[self.api.head][schema.CATALOG_PATH] = json.dumps({
            "schema_version": 1, "project_ids": ["local-agent", "example-project"],
        })
        with self.assertRaises(ValueError):
            self.observe()
        self.api.snapshots[self.api.head][schema.CATALOG_PATH] = json.dumps({
            "schema_version": 1, "project_ids": ["example-project", "example-project"],
        })
        with self.assertRaises(ValueError):
            self.observe()
        self.api.snapshots[self.api.head][schema.CATALOG_PATH] = json.dumps({
            "schema_version": 1, "project_ids": ["example-project"],
        })
        path = "projects/example-project/workflows/index.json"
        self.api.snapshots[self.api.head][path] = json.dumps({
            "schema_version": 1, "workflow_ids": ["workflow-002", "workflow-001"],
        })
        with self.assertRaises(ValueError):
            self.observe()


if __name__ == "__main__":
    unittest.main()
