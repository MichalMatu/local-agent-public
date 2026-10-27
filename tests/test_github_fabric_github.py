"""Synthetic GitHub Fabric publication round trip with an isolated Git Data API."""

from __future__ import annotations

import base64
import contextlib
import copy
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from urllib.error import HTTPError

from local_agent.conversation import github_fabric_github as publisher
from tests.test_github_fabric_dispatch import admitted_children, operator_request


class FakeGitDataAPI:
    """Commit-pinned Git Data API with a rejecting fast-forward-only ref."""

    def __init__(self) -> None:
        self.head = "a" * 40
        self.snapshots = {self.head: {}}
        self.trees = {"b" * 40: {}}
        self.commit_trees = {self.head: "b" * 40}
        self.blobs = {}
        self.commits = {}
        self.counter = 0
        self.operations = []
        self.fail_after_ref = False
        self.fail_after_ref_number = None
        self.ref_successes = 0
        self.reject_next_ref = False
        self.deny_next_request = False
        self.corrupt_path = None

    def new_sha(self) -> str:
        self.counter += 1
        return f"{self.counter:040x}"

    def _advance_out_of_band(self) -> None:
        head = self.new_sha()
        self.snapshots[head] = copy.deepcopy(self.snapshots[self.head])
        self.commit_trees[head] = self.commit_trees[self.head]
        self.head = head

    def request(self, method, path, body=None):
        self.operations.append((method, path, copy.deepcopy(body)))
        if self.deny_next_request:
            self.deny_next_request = False
            raise publisher.GithubFabricHTTPError(403)
        if method == "GET" and path == publisher.GITHUB_REF_PATH:
            return {"object": {"sha": self.head}}
        if method == "GET" and path.startswith("/git/commits/"):
            sha = path.split("/")[-1]
            return {"tree": {"sha": self.commit_trees[sha]}}
        if method == "GET" and path.startswith("/contents/"):
            relative, _, ref = path[len("/contents/"):].partition("?ref=")
            if relative == self.corrupt_path:
                return {"type": "file", "encoding": "base64", "size": 2, "content": "***"}
            data = self.snapshots[ref].get(relative)
            if data is None:
                raise publisher.GithubFabricHTTPError(404)
            encoded = data.encode("utf-8")
            return {
                "type": "file", "encoding": "base64",
                "size": len(encoded), "content": base64.b64encode(encoded).decode("ascii"),
            }
        if method == "POST" and path == "/git/blobs":
            sha = self.new_sha()
            self.blobs[sha] = body["content"]
            return {"sha": sha}
        if method == "POST" and path == "/git/trees":
            sha = self.new_sha()
            contents = copy.deepcopy(self.trees[body["base_tree"]])
            entries = body["tree"]
            if len(entries) != 1 or entries[0]["mode"] != "100644":
                raise AssertionError("expected one scoped immutable write")
            contents[entries[0]["path"]] = self.blobs[entries[0]["sha"]]
            self.trees[sha] = contents
            return {"sha": sha}
        if method == "POST" and path == "/git/commits":
            sha = self.new_sha()
            self.commits[sha] = body
            self.commit_trees[sha] = body["tree"]
            return {"sha": sha}
        if method == "PATCH" and path == publisher.GITHUB_REF_UPDATE_PATH:
            if body.get("force") is not False:
                raise AssertionError("force-updating the ref is forbidden")
            if self.reject_next_ref:
                self.reject_next_ref = False
                self._advance_out_of_band()
            commit = self.commits[body["sha"]]
            if commit["parents"] != [self.head]:
                raise publisher.GithubFabricHTTPError(422)
            self.head = body["sha"]
            self.snapshots[self.head] = copy.deepcopy(self.trees[commit["tree"]])
            self.ref_successes += 1
            if self.fail_after_ref or self.fail_after_ref_number == self.ref_successes:
                self.fail_after_ref = False
                raise publisher.GithubFabricTransportError("simulated lost acknowledgement")
            return {"ref": "refs/heads/chat-bridge-state"}
        raise AssertionError((method, path))


class GithubFabricTrustedWriterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.api = FakeGitDataAPI()
        self.request = operator_request()
        self.children = admitted_children()

    def publish(self, **changes):
        kw = {"api": self.api, "enabled": True}
        kw.update(changes)
        return publisher.publish_synthetic_fixture(self.request, self.children, **kw)

    def test_disabled_by_default_and_no_token_fail_closed(self):
        with self.assertRaises(PermissionError):
            publisher.publish_synthetic_fixture(self.request, self.children, api=self.api)
        self.assertEqual(self.api.operations, [])
        with self.assertRaises(PermissionError):
            publisher.publish_synthetic_fixture(self.request, self.children, enabled=True)
        self.assertEqual(self.api.operations, [])
        with self.assertRaises(PermissionError):
            publisher.GitHubFabricREST("")

    def test_github_ref_read_and_update_endpoints_are_distinct(self):
        self.assertEqual(publisher.GITHUB_REF_PATH, "/git/ref/heads/chat-bridge-state")
        self.assertEqual(publisher.GITHUB_REF_UPDATE_PATH, "/git/refs/heads/chat-bridge-state")

    def test_full_record_first_round_trip_and_restart_replay(self):
        result = self.publish()
        self.assertEqual(result.status, "published")
        self.assertEqual(result.applied_steps, ("create_record", "update_index"))
        self.assertEqual(len(self.api.commits), 2)
        self.assertEqual(
            [entry["message"] for entry in self.api.commits.values()],
            ["Synthetic GitHub Fabric create_record", "Synthetic GitHub Fabric update_index"]
        )
        record_path = ".agent/conversation/browser_dispatches/" + result.dispatch_id + ".json"
        remote = self.api.snapshots[self.api.head]
        self.assertEqual(json.loads(remote[record_path])["id"], result.dispatch_id)
        self.assertEqual(json.loads(remote[publisher.preflight.INDEX_PATH])["dispatch_ids"], [result.dispatch_id])
        self.assertEqual(self.api.head, result.head_sha)
        commits_before = len(self.api.commits)
        replay = self.publish()
        self.assertEqual(replay.status, "replay")
        self.assertEqual(replay.applied_steps, ())
        self.assertEqual(len(self.api.commits), commits_before)

    def test_record_acknowledgement_lost_reconciles_without_second_record(self):
        self.api.fail_after_ref = True
        result = self.publish()
        self.assertEqual(result.status, "published")
        self.assertEqual(result.applied_steps, ("update_index",))
        self.assertEqual(len(self.api.commits), 2)

    def test_index_acknowledgement_lost_reconciles_without_replay(self):
        # Record succeeds, index Git ref advances but its response is lost.
        self.api.fail_after_ref_number = 2
        result = self.publish()
        self.assertEqual(result.status, "published")
        self.assertEqual(result.applied_steps, ("create_record",))
        self.assertEqual(len(self.api.commits), 2)
        index = json.loads(self.api.snapshots[self.api.head][publisher.preflight.INDEX_PATH])
        self.assertEqual(index["dispatch_ids"], [result.dispatch_id])
        self.assertEqual(self.publish().status, "replay")
        self.assertEqual(len(self.api.commits), 2)

    def test_non_fast_forward_rechecks_new_origin_without_force(self):
        self.api.reject_next_ref = True
        result = self.publish()
        self.assertEqual(result.status, "published")
        self.assertEqual(result.applied_steps, ("create_record", "update_index"))
        self.assertGreaterEqual(len(self.api.commits), 3)
        ref_updates = [entry for entry in self.api.operations if entry[0] == "PATCH"]
        self.assertGreaterEqual(len(ref_updates), 3)
        self.assertTrue(all(entry[2]["force"] is False for entry in ref_updates))

    def test_denied_access_does_not_write_and_fails_closed(self):
        self.api.deny_next_request = True
        with self.assertRaises(publisher.GithubFabricHTTPError) as raised:
            self.publish()
        self.assertEqual(raised.exception.status, 403)
        self.assertEqual(len(self.api.commits), 0)

    def test_private_payload_and_remote_corruption_never_write(self):
        self.children[0]["scope"]["summary"] = "Client secret"
        with self.assertRaises(PermissionError):
            self.publish()
        self.assertEqual(len(self.api.commits), 0)
        self.children = admitted_children()
        self.api.corrupt_path = publisher.preflight.INDEX_PATH
        with self.assertRaises(ValueError):
            self.publish()
        self.assertEqual(len(self.api.commits), 0)

    def test_conflicting_existing_record_and_full_index_fail_closed(self):
        candidate = self.publish()
        path = publisher.preflight.RECORD_ROOT + candidate.dispatch_id + ".json"
        changed = json.loads(self.api.snapshots[self.api.head][path])
        changed["children"][0]["spawn"]["bootstrap_text"] += "\nchanged"
        self.api.snapshots[self.api.head][path] = json.dumps(changed)
        with self.assertRaisesRegex(ValueError, "same-id dispatch conflict"):
            self.publish()
        self.assertEqual(len(self.api.commits), 2)

    def test_ref_rejection_exhaustion_fails_closed(self):
        api = self.api

        class AlwaysConflict:
            def request(self, method, path, body=None):
                if method == "PATCH":
                    raise publisher.GithubFabricHTTPError(422)
                return api.request(method, path, body)

        with self.assertRaisesRegex(RuntimeError, "did not converge"):
            self.publish(api=AlwaysConflict())
        self.assertEqual(api.head, "a" * 40)

    def test_remote_malformed_ref_does_not_advance(self):
        class InvalidRef:
            def request(self, method, path, body=None):
                return {"object": {"sha": "invalid"}}

        with self.assertRaisesRegex(ValueError, "branch head SHA"):
            self.publish(api=InvalidRef())
        self.assertEqual(len(self.api.commits), 0)

    def test_trusted_rest_denies_redirect_before_reusing_bearer_token(self):
        adapter = publisher.GitHubFabricREST("synthetic-secret-token")
        with mock.patch.object(publisher, "build_opener") as build:
            build.return_value.open.side_effect = HTTPError(
                "https://api.github.com/", 302, "Moved",
                {"Location": "https://external.invalid/token"}, None,
            )
            with self.assertRaises(publisher.GithubFabricHTTPError) as raised:
                adapter.request("GET", publisher.GITHUB_REF_PATH)
            self.assertEqual(raised.exception.status, 302)
            self.assertEqual(build.call_count, 1)
            handler = build.call_args.args[0]
            self.assertIsInstance(handler, publisher._NoRedirect)
            self.assertIsNone(handler.redirect_request(
                None, None, 302, "Moved", {},
                "https://external.invalid/token",
            ))
            sent = build.return_value.open.call_args.args[0]
            self.assertEqual(sent.full_url,
                             publisher.GITHUB_API_ROOT + publisher.GITHUB_REF_PATH)
            self.assertEqual(sent.get_header("Authorization"),
                             "Bearer synthetic-secret-token")

    def test_public_rest_rejects_unsafe_ref_payloads(self):
        api = publisher.GitHubFabricREST("synthetic-test-token")
        with mock.patch.object(publisher, "build_opener") as opener:
            for payload in (
                None, {}, {"sha": "a" * 40, "force": True},
                {"sha": "a" * 40, "force": 0},
                {"sha": "bad", "force": False},
                {"sha": ["a" * 40], "force": False},
                {"sha": "a" * 40, "force": False, "extra": True},
            ):
                with self.subTest(payload=payload), self.assertRaises(ValueError):
                    api.request("PATCH", publisher.GITHUB_REF_UPDATE_PATH, payload)
            opener.assert_not_called()

    def test_publisher_rejects_invalid_header_token(self):
        for token in ("", "token\r\nBearer evil", " token", "token ", "tok\nen"):
            with self.subTest(token=token), self.assertRaises(PermissionError):
                publisher.GitHubFabricREST(token)

    def test_cli_requires_explicit_public_consent_and_token(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "synthetic.json"
            source.write_text(json.dumps({
                "operator_request": self.request,
                "child_requests": self.children,
            }), encoding="utf-8")
            with mock.patch.dict(os.environ, {}, clear=True):
                with self.assertRaises(SystemExit):
                    publisher.main(["--source", str(source)])
                with self.assertRaises(SystemExit):
                    publisher.main(["--source", str(source), "--publish-public-synthetic"])
            with mock.patch.dict(
                os.environ, {"LOCAL_AGENT_GITHUB_FABRIC_WRITE_TOKEN": "fixture-token"}
            ), mock.patch.object(
                publisher, "publish_synthetic_fixture",
                return_value=publisher.SyntheticPublicationResult(
                    "fabric-" + "a" * 32, "published", "a" * 40, ("create_record", "update_index")
                ),
            ) as publish:
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    result = publisher.main([
                        "--source", str(source), "--publish-public-synthetic"
                    ])
                self.assertEqual(result, 0)
                self.assertEqual(json.loads(output.getvalue())["status"], "published")
                self.assertNotIn("fixture-token", output.getvalue())
                publish.assert_called_once_with(
                    self.request, self.children, enabled=True, token="fixture-token"
                )



if __name__ == "__main__":
    unittest.main()
