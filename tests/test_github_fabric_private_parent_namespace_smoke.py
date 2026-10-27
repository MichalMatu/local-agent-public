"""GET-only live private parent namespace absence verification contract."""

from __future__ import annotations

import copy
import json
import subprocess
import unittest
from contextlib import redirect_stdout
from io import StringIO
from unittest import mock

from local_agent.conversation import github_fabric_private_live_smoke as smoke


class FakePrivateParentOrigin:
    head = "a" * 40
    tree_sha = "b" * 40

    def __init__(self):
        self.calls = []
        self.entries = [
            {"path": "projects", "type": "tree"},
            {"path": "projects/index.json", "type": "blob"},
        ]
        self.bad_ref = False
        self.truncated = False
        self.bad_tree_sha = False
        self.move_head_on_second = False

    def request(self, method, path, body=None):
        self.calls.append((method, path, body))
        if method != "GET" or body is not None:
            raise AssertionError("read-only parent smoke attempted a write")
        if path == "/git/ref/heads/fabric-data":
            origin = ("c" * 40 if self.move_head_on_second
                      and len([c for c in self.calls if c[1] == path]) > 1
                      else self.head)
            return {
                "ref": "refs/heads/main" if self.bad_ref else "refs/heads/fabric-data",
                "object": {"type": "commit", "sha": origin},
            }
        if path in {"/git/commits/" + self.head, "/git/commits/" + "c" * 40}:
            return {"tree": {"sha": self.tree_sha}}
        if path == "/git/trees/" + self.tree_sha + "?recursive=1":
            return {
                "sha": ("0" * 40 if self.bad_tree_sha else self.tree_sha),
                "truncated": self.truncated,
                "tree": copy.deepcopy(self.entries),
            }
        raise AssertionError("unexpected path: " + path)


class ParentNamespaceSmokeTests(unittest.TestCase):
    def test_default_disabled_without_private_requests(self):
        api = FakePrivateParentOrigin()
        with self.assertRaisesRegex(PermissionError, "disabled"):
            smoke.verify_private_parent_namespace_empty(api=api)
        self.assertEqual(api.calls, [])

    def test_two_commit_pinned_get_only_reads_without_browser_authority(self):
        api = FakePrivateParentOrigin()
        result = smoke.verify_private_parent_namespace_empty(enabled=True, api=api)
        self.assertEqual(result.source_head_sha, api.head)
        self.assertEqual(result.reads, 2)
        self.assertEqual(result.status, "no_parent_records_observed")
        self.assertIs(result.browser_effects_permitted, False)
        self.assertEqual([path for _, path, _ in api.calls], [
            "/git/ref/heads/fabric-data",
            "/git/commits/" + api.head,
            "/git/trees/" + api.tree_sha + "?recursive=1",
        ] * 2)
        self.assertTrue(all(method == "GET" and body is None
                            for method, _, body in api.calls))

    def test_present_parent_index_or_record_refuses_empty_claim(self):
        for name in ("parents/index.json", "parents/parent-" + "f" * 32 + ".json"):
            with self.subTest(path=name):
                api = FakePrivateParentOrigin()
                api.entries.append({"path": "parents", "type": "tree"})
                api.entries.append({"path": name, "type": "blob"})
                with self.assertRaisesRegex(ValueError, "manual review"):
                    smoke.verify_private_parent_namespace_empty(enabled=True, api=api)

    def test_unknown_parent_path_and_bad_namespace_directory_fail_closed(self):
        api = FakePrivateParentOrigin()
        api.entries.append({"path": "parents/private-token.txt", "type": "blob"})
        with self.assertRaisesRegex(ValueError, "unexpected path"):
            smoke.verify_private_parent_namespace_empty(enabled=True, api=api)
        api = FakePrivateParentOrigin()
        api.entries.append({"path": "parents", "type": "blob"})
        with self.assertRaisesRegex(ValueError, "root corrupt"):
            smoke.verify_private_parent_namespace_empty(enabled=True, api=api)

    def test_partial_corrupt_and_moving_origin_fail_closed(self):
        cases = [
            ("truncated", True, "incomplete"),
            ("bad_tree_sha", True, "incomplete"),
            ("bad_ref", True, "ref invalid"),
            ("move_head_on_second", True, "changed across"),
        ]
        for field, value, message in cases:
            with self.subTest(field=field):
                api = FakePrivateParentOrigin()
                setattr(api, field, value)
                with self.assertRaisesRegex(ValueError, message):
                    smoke.verify_private_parent_namespace_empty(enabled=True, api=api)
        api = FakePrivateParentOrigin()
        api.entries = [{"name": "no path"}]
        with self.assertRaisesRegex(ValueError, "entry invalid"):
            smoke.verify_private_parent_namespace_empty(enabled=True, api=api)
        api = FakePrivateParentOrigin()
        api.entries = [{"path": "projects"}] * (smoke.MAX_TREE_ENTRIES + 1)
        with self.assertRaisesRegex(ValueError, "incomplete"):
            smoke.verify_private_parent_namespace_empty(enabled=True, api=api)

    def test_cli_allowlist_accepts_only_exact_private_recursive_tree_get(self):
        body = json.dumps({"sha": "a" * 40, "truncated": False, "tree": []}).encode()
        commands = []

        def runner(argv, **kwargs):
            commands.append((argv, kwargs))
            return subprocess.CompletedProcess(argv, 0, body, b"private stderr secret")

        cli = smoke.GithubCliReadOnlyAdapter(run=runner)
        path = "/git/trees/" + "a" * 40 + "?recursive=1"
        self.assertEqual(cli.request("GET", path)["sha"], "a" * 40)
        self.assertEqual(commands[0][0], [
            "gh", "api", "--method", "GET",
            "repos/example-owner/local-agent-fabric-private" + path,
        ])
        for request in (
            ("GET", "/git/trees/" + "a" * 40),
            ("GET", "/git/trees/" + "a" * 40 + "?recursive=0"),
            ("GET", "/git/trees/" + "a" * 40 + "?recursive=1&unexpected=1"),
            ("GET", "/git/trees/../main?recursive=1"),
            ("POST", path),
        ):
            with self.subTest(request=request), self.assertRaises(PermissionError):
                cli.request(*request)
        self.assertEqual(len(commands), 1)

    def test_cli_output_never_exposes_private_content_or_acks(self):
        sample = smoke.ReadOnlyPrivateParentNamespaceSmoke(
            "a" * 40, "no_parent_records_observed", 2, False
        )
        out = StringIO()
        with mock.patch.object(
            smoke, "verify_private_parent_namespace_empty", return_value=sample
        ) as verified, redirect_stdout(out):
            self.assertEqual(smoke.main(["--verify-private-parent-namespace-empty"]), 0)
        verified.assert_called_once_with(enabled=True)
        data = json.loads(out.getvalue())
        self.assertEqual(data["source_head_sha"], "a" * 40)
        self.assertEqual(data["status"], "no_parent_records_observed")
        self.assertEqual(data["ack_state"], "not_attested")
        self.assertIs(data["browser_effects_permitted"], False)
        self.assertNotIn("token", data)
        self.assertNotIn("bootstrap_text", data)
        with self.assertRaises(SystemExit):
            smoke.main([
                "--verify-private-parent-namespace-empty",
                "--verify-private-catalog-read",
            ])


if __name__ == "__main__":
    unittest.main()
