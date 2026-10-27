"""Real GitHub private synthetic smoke contract without credential exposure."""

from __future__ import annotations

import json
import subprocess
import unittest
from unittest import mock

from local_agent.conversation import github_fabric_private_github as private_api
from local_agent.conversation import github_fabric_private_live_smoke as smoke
from tests.test_github_fabric_dispatch import admitted_children, operator_request
from tests.test_github_fabric_private_github import PrivateGitDataAPI


class AuthenticatedFakeCli:
    def __init__(self):
        self.git = PrivateGitDataAPI()
        private_api.publish_private_synthetic_fixture(
            operator_request(), admitted_children(), enabled=True, api=self.git,
        )
        self.calls = []
        self.fail = False

    def __call__(self, argv, **kwargs):
        self.calls.append((tuple(argv), kwargs))
        assert argv[:4] == ["gh", "api", "--method", "GET"]
        assert len(argv) == 5
        assert kwargs == {"capture_output": True, "timeout": 25, "check": False}
        prefix = "repos/example-owner/local-agent-fabric-private"
        assert argv[4].startswith(prefix)
        if self.fail:
            return subprocess.CompletedProcess(argv, 1, b"", b"SECRET-DO-NOT-LOG")
        path = argv[4][len(prefix):]
        result = self.git.request("GET", path)
        return subprocess.CompletedProcess(
            argv, 0, json.dumps(result, sort_keys=True).encode("utf-8"), b"",
        )


class GithubCliPrivateSmokeTests(unittest.TestCase):
    def test_default_disabled_before_cli_or_private_io(self):
        with self.assertRaises(PermissionError):
            smoke.verify_private_synthetic_live_read()
        fake = AuthenticatedFakeCli()
        fake.calls.clear()
        with self.assertRaises(PermissionError):
            smoke.verify_private_synthetic_live_read(
                api=smoke.GithubCliReadOnlyAdapter(run=fake)
            )
        self.assertEqual(fake.calls, [])
        with self.assertRaises(SystemExit):
            smoke.main([])

    def test_genuine_two_pinned_read_reconstruction_through_cli_adapter(self):
        fake = AuthenticatedFakeCli()
        fake.calls.clear()
        api = smoke.GithubCliReadOnlyAdapter(run=fake)
        result = smoke.verify_private_synthetic_live_read(enabled=True, api=api)
        self.assertEqual(result.source_head_sha, fake.git.head)
        self.assertEqual(result.child_count, 2)
        self.assertEqual(result.reads, 2)
        self.assertEqual(result.execution_state, "published_execution_unconfirmed")
        self.assertEqual(len(fake.calls), 12)
        self.assertTrue(all(argv[2:4] == ("--method", "GET") for argv, _ in fake.calls))
        self.assertEqual(len(fake.git.commits), 3)
        self.assertTrue(all(
            argv[-1].startswith("repos/example-owner/local-agent-fabric-private/")
            for argv, _ in fake.calls
        ))
        self.assertTrue(all("token" not in str(argv).lower() for argv, _ in fake.calls))

    def test_cli_rejects_mutations_tokens_cross_repo_and_path_traversal_before_call(self):
        fake = AuthenticatedFakeCli()
        fake.calls.clear()
        api = smoke.GithubCliReadOnlyAdapter(run=fake)
        rejects = [
            ("POST", "/git/blobs", {}),
            ("PATCH", "/git/refs/heads/fabric-data", {"force": True}),
            ("GET", "/git/commits/" + "a" * 40 + "?test=1", None),
            ("GET", "/contents/projects/example-project/workflows/../../index.json?ref=" + "a" * 40, None),
            ("GET", "/contents/../private?ref=" + "a" * 40, None),
            ("GET", "/git/ref/heads/main", None),
            ("GET", "/contents/projects/index.json?ref=../main", None),
            ("GET", "/contents/projects/local-agent/workflows/workflow-001/dispatches/"
             "fabric-" + "0" * 32 + ".json?ref=main", None),
        ]
        for method, path, body in rejects:
            with self.subTest(path=path, method=method), self.assertRaises(PermissionError):
                api.request(method, path, body)
        self.assertEqual(fake.calls, [])

    def test_error_stderr_and_response_contents_do_not_leak(self):
        fake = AuthenticatedFakeCli()
        fake.fail = True
        api = smoke.GithubCliReadOnlyAdapter(run=fake)
        with self.assertRaisesRegex(RuntimeError, "read failed") as error:
            api.request("GET", private_api.REF_PATH)
        self.assertNotIn("SECRET-DO-NOT-LOG", str(error.exception))
        self.assertEqual(fake.calls[0][0][:4], ("gh", "api", "--method", "GET"))
        def oversized(argv, **kwargs):
            return subprocess.CompletedProcess(
                argv, 0, b" " * (smoke.MAX_GH_RESPONSE_BYTES + 1), b"secret"
            )
        with self.assertRaisesRegex(ValueError, "byte bound"):
            smoke.GithubCliReadOnlyAdapter(run=oversized).request(
                "GET", private_api.REF_PATH
            )
        def malformed(argv, **kwargs):
            return subprocess.CompletedProcess(
                argv, 0, b"{malformed}", b"secret"
            )
        with self.assertRaisesRegex(ValueError, "malformed JSON"):
            smoke.GithubCliReadOnlyAdapter(run=malformed).request(
                "GET", private_api.REF_PATH
            )

    def test_invalid_or_missing_private_record_cannot_be_interpreted_as_success(self):
        fake = AuthenticatedFakeCli()
        dispatch_index = "projects/local-agent/workflows/workflow-001/dispatches/index.json"
        index = json.loads(fake.git.snapshots[fake.git.head][dispatch_index])
        file = ("projects/local-agent/workflows/workflow-001/dispatches/"
                + index["dispatch_ids"][0] + ".json")
        fake.git.snapshots[fake.git.head].pop(file)
        with self.assertRaisesRegex(ValueError, "indexed dispatch record missing"):
            smoke.verify_private_synthetic_live_read(
                enabled=True, api=smoke.GithubCliReadOnlyAdapter(run=fake)
            )

    def test_cli_only_prints_bounded_identifiers_not_private_text(self):
        sample = smoke.ReadOnlyPrivateSyntheticSmoke(
            "a" * 40, "fabric-" + "b" * 32, 2, 2,
            "published_execution_unconfirmed"
        )
        with mock.patch.object(
            smoke, "verify_private_synthetic_live_read", return_value=sample
        ) as verified:
            from contextlib import redirect_stdout
            from io import StringIO
            output = StringIO()
            with redirect_stdout(output):
                self.assertEqual(
                    smoke.main(["--verify-private-synthetic-read"]), 0,
                )
        verified.assert_called_once_with(enabled=True)
        data = json.loads(output.getvalue())
        self.assertEqual(data["ack_state"], "not_attested")
        self.assertEqual(data["reads"], 2)
        self.assertEqual(data["child_count"], 2)
        self.assertNotIn("bootstrap_text", data)
        self.assertNotIn("token", data)


def populate_fake_project_catalog(fake):
    for project in ("example-project", "example-device"):
        key = f"projects/{project}/workflows/index.json"
        fake.git.snapshots[fake.git.head][key] = json.dumps({
            "schema_version": 1, "workflow_ids": [],
        })


class GithubCliPrivateCatalogTests(unittest.TestCase):
    def test_disabled_before_authenticated_cli_get(self):
        fake = AuthenticatedFakeCli()
        fake.calls.clear()
        with self.assertRaises(PermissionError):
            smoke.verify_private_project_catalog_live_read(
                api=smoke.GithubCliReadOnlyAdapter(run=fake),
            )
        self.assertEqual(fake.calls, [])

    def test_all_indexed_projects_recovered_twice_using_only_get(self):
        fake = AuthenticatedFakeCli()
        populate_fake_project_catalog(fake)
        fake.calls.clear()
        reader = smoke.GithubCliReadOnlyAdapter(run=fake)
        catalog = smoke.verify_private_project_catalog_live_read(
            enabled=True, api=reader,
        )
        self.assertEqual(catalog.source_head_sha, fake.git.head)
        self.assertEqual(
            {p.project_id: list(p.workflow_ids) for p in catalog.projects},
            {"example-project": [], "local-agent": ["workflow-001"], "example-device": []},
        )
        self.assertEqual(len(fake.calls), 10)
        self.assertTrue(all(argv[2:4] == ("--method", "GET") for argv, _ in fake.calls))
        self.assertTrue(all("token" not in str(argv).lower() for argv, _ in fake.calls))
        self.assertNotIn("bootstrap_text", str(catalog))

    def test_cli_prints_only_project_and_workflow_identifiers(self):
        fake = AuthenticatedFakeCli()
        populate_fake_project_catalog(fake)
        observed = smoke.verify_private_project_catalog_live_read(
            enabled=True, api=smoke.GithubCliReadOnlyAdapter(run=fake),
        )
        with mock.patch.object(
            smoke, "verify_private_project_catalog_live_read",
            return_value=observed,
        ) as verified:
            from contextlib import redirect_stdout
            from io import StringIO
            output = StringIO()
            with redirect_stdout(output):
                self.assertEqual(smoke.main(["--verify-private-catalog-read"]), 0)
        verified.assert_called_once_with(enabled=True)
        result = json.loads(output.getvalue())
        self.assertEqual(result["reads"], 2)
        self.assertEqual(result["source_kind"], "private_project_catalog_observation_only")
        self.assertEqual(result["projects"]["local-agent"], ["workflow-001"])
        self.assertNotIn("bootstrap_text", result)
        self.assertNotIn("token", result)
        with self.assertRaises(SystemExit):
            smoke.main(["--verify-private-synthetic-read", "--verify-private-catalog-read"])


if __name__ == "__main__":
    unittest.main()
