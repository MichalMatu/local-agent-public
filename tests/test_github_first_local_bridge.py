"""GitHub-first transport smoke: real private data fixture, local Host Ops spool."""

from __future__ import annotations

import copy
import http.client
import json
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path

from local_agent.conversation import github_fabric_dispatch as dispatch_contract
from local_agent.conversation import github_fabric_private_publication as catalog
from local_agent.conversation import github_first_bridge, github_first_results
from local_agent.host_ops.capabilities.local.browser.chat_bridge_http import make_bridge_handler
from local_agent.host_ops.capabilities.local.browser.chat_bridge_spool import ChatBridgeSpool
from tests.test_github_fabric_dispatch import admitted_children, operator_request
from tests.test_github_fabric_private_live import AtomicPrivateGitDataAPI


class GithubFirstBridgeTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.spool = ChatBridgeSpool(Path(self.directory.name) / "bridge.json")
        self.api = AtomicPrivateGitDataAPI()
        self.dispatch = dispatch_contract.build_github_fabric_dispatch(operator_request(), admitted_children())
        source = self.api.snapshots[self.api.head]
        source[catalog.WORKFLOWS_PATH] = json.dumps({"schema_version": 1, "workflow_ids": [catalog.WORKFLOW_ID]})
        source[catalog.INDEX_PATH] = json.dumps({"schema_version": 1, "dispatch_ids": [self.dispatch["id"]]})
        source[catalog.dispatch_path(self.dispatch["id"])] = json.dumps(self.dispatch)

    def test_one_child_complete_transport_no_dom_result(self):
        with self.assertRaises(PermissionError):
            github_first_bridge.admit_dispatch(self.dispatch["id"], self.spool, api=self.api)
        self.assertEqual(self.api.operations, [])
        admitted = github_first_bridge.admit_dispatch(self.dispatch["id"], self.spool, enabled=True, api=self.api)
        self.assertEqual(admitted.source_head, self.api.head)
        self.assertEqual(admitted.phases, ("queued", "queued"))
        first = self.spool.take()
        self.assertEqual(first["intent"]["bootstrap_text"], self.dispatch["children"][0]["spawn"]["bootstrap_text"])
        self.assertIsNotNone(self.spool.take())
        self.assertIsNone(self.spool.take())
        transaction = first["intent"]["transaction_id"]
        self.assertEqual(self.spool.status()[transaction]["phase"], "submission_unknown")
        child_url = "https://chatgpt.com/c/child-test"
        self.assertEqual(self.spool.acknowledge(transaction, child_url), "acknowledged")
        self.assertEqual(self.spool.status()[transaction]["child_url"], child_url)
        self.assertEqual(self.spool.acknowledge(transaction, child_url), "acknowledged")
        self.assertEqual(github_first_bridge.admit_dispatch(
            self.dispatch["id"], self.spool, enabled=True, api=self.api
        ).phases, ("acknowledged", "submission_unknown"))
        # No browser content, DOM reply or false child result is stored by Host Ops.
        self.assertNotIn("assistant_text", repr(self.spool.status()))

    def test_immutable_collision_unpublished_dispatch(self):
        github_first_bridge.admit_dispatch(self.dispatch["id"], self.spool, enabled=True, api=self.api)
        altered = copy.deepcopy(self.dispatch)
        altered["children"][0]["spawn"]["bootstrap_text"] += " changed"
        self.api.snapshots[self.api.head][catalog.dispatch_path(self.dispatch["id"])] = json.dumps(altered)
        with self.assertRaises(ValueError):
            github_first_bridge.admit_dispatch(self.dispatch["id"], self.spool, enabled=True, api=self.api)
        self.assertEqual(len(self.spool.status()), 2)
        with self.assertRaises(ValueError):
            self.spool.acknowledge("spawn-" + "1" * 64, "https://chatgpt.com/c/unclaimed")

    def test_missing_catalog_and_unknown_id_refused(self):
        del self.api.snapshots[self.api.head][catalog.INDEX_PATH]
        with self.assertRaises(ValueError):
            github_first_bridge.admit_dispatch(self.dispatch["id"], self.spool, enabled=True, api=self.api)
        self.assertIsNone(self.spool.take())

    def test_full_github_first_dispatch_results_parent_wake(self):
        start = github_first_bridge.sync_indexed_private_dispatches(self.spool, api=self.api)
        self.assertEqual(start["phases"][self.dispatch["id"]], ("queued", "queued"))
        self.assertEqual(start["completed_ids"][self.dispatch["id"]], ())
        # The children, not the Bridge, publish these immutable private GitHub files.
        for child in self.dispatch["children"]:
            self.api.snapshots[self.api.head][github_first_results.result_path(self.dispatch, child)] = json.dumps({
                "schema_version": 1, "kind": "child_agent_result",
                "dispatch_id": self.dispatch["id"], "child_request_id": child["request_id"],
                "spawn_transaction_id": child["spawn"]["transaction_id"],
                "status": "completed", "result_text": "Verified child work"
            })
        end = github_first_bridge.sync_indexed_private_dispatches(self.spool, api=self.api)
        self.assertEqual(set(end["completed_ids"][self.dispatch["id"]]), {
            child["request_id"] for child in self.dispatch["children"]
        })
        self.assertEqual(end["phases"][self.dispatch["id"]], ("result_published", "result_published"))
        items = self.spool.status()
        wakes = [key for key in items if key.startswith("wake-")]
        self.assertEqual(len(wakes), 1)
        self.assertEqual(github_first_bridge.sync_indexed_private_dispatches(
            self.spool, api=self.api
        )["phases"][self.dispatch["id"]], ("result_published", "result_published"))
        # Drain child transport instructions first, then the one parent wake.
        one = self.spool.take()
        two = self.spool.take()
        three = self.spool.take()
        self.assertEqual({one["child_request_id"], two["child_request_id"]}, {
            child["request_id"] for child in self.dispatch["children"]
        })
        self.assertEqual(three["kind"], "parent_wake")
        self.assertEqual(self.spool.acknowledge_parent_wake(three["wake_id"]), "acknowledged")
        self.assertIsNone(self.spool.take())
        self.assertNotIn("Verified child work", repr(self.spool.status()))

    def test_pilot_selects_only_one_indexed_dispatch_without_staging_history(self):
        old_id = "fabric-" + "f" * 32
        # Historical index entries exist in the live private branch. They must
        # neither get read nor staged by a pilot scoped to this dispatch.
        source = self.api.snapshots[self.api.head]
        source[catalog.INDEX_PATH] = json.dumps({
            "schema_version": 1,
            "dispatch_ids": sorted([old_id, self.dispatch["id"]]),
        })
        scoped = github_first_bridge.sync_indexed_private_dispatches(
            self.spool, api=self.api, dispatch_id=self.dispatch["id"]
        )
        self.assertEqual(set(scoped["phases"]), {self.dispatch["id"]})
        self.assertEqual(len(self.spool.status()), len(self.dispatch["children"]))
        with self.assertRaisesRegex(ValueError, "not in the private index"):
            github_first_bridge.sync_indexed_private_dispatches(
                self.spool, api=self.api, dispatch_id="fabric-" + "0" * 32
            )
        self.assertEqual(len(self.spool.status()), len(self.dispatch["children"]))

    def test_source_runner_requires_explicit_dispatch_for_pilot(self):
        with self.assertRaises(SystemExit) as error:
            github_first_bridge.main(["--sync-once", "--spool", str(self.spool.path)])
        self.assertEqual(error.exception.code, 2)
        self.assertEqual(self.spool.status(), {})

    def test_global_first_start_fences_history_and_tracks_new_dispatches(self):
        cursor = Path(self.directory.name) / "intake.json"
        start = github_first_bridge.sync_future_private_dispatches(
            self.spool, api=self.api, cursor_path=cursor)
        self.assertEqual(start["ignored_count"], 1)
        self.assertEqual(start["tracked_count"], 0)
        self.assertEqual(self.spool.status(), {})
        old = self.dispatch
        new = copy.deepcopy(old)
        new["id"] = "fabric-" + "9" * 32
        new["request_id"] = "new-operator-request"
        new["children"] = new["children"][:1]
        new["children"][0]["spawn"]["transaction_id"] = "spawn-" + "c" * 64
        source = self.api.snapshots[self.api.head]
        source[catalog.INDEX_PATH] = json.dumps({
            "schema_version": 1, "dispatch_ids": sorted([old["id"], new["id"]])
        })
        source[catalog.dispatch_path(new["id"])] = json.dumps(new)
        added = github_first_bridge.sync_future_private_dispatches(
            self.spool, api=self.api, cursor_path=cursor)
        self.assertEqual(added["tracked_count"], 1)
        self.assertEqual(set(added["phases"]), {new["id"]})
        self.assertEqual(len(self.spool.status()), 1)
        item = self.spool.take()
        self.assertEqual(item["dispatch_id"], new["id"])
        after_restart = github_first_bridge.sync_future_private_dispatches(
            self.spool, api=self.api, cursor_path=cursor)
        self.assertEqual(after_restart["tracked_count"], 1)
        self.assertEqual(len(self.spool.status()), 1)
        self.assertEqual(self.spool.take(), None)
        self.assertEqual(cursor.stat().st_mode & 0o777, 0o600)

    def test_global_intake_refuses_malformed_cursor_without_browser_effect(self):
        cursor = Path(self.directory.name) / "intake.json"
        cursor.write_text("{}")
        cursor.chmod(0o600)
        with self.assertRaises(ValueError):
            github_first_bridge.sync_future_private_dispatches(
                self.spool, api=self.api, cursor_path=cursor)
        self.assertEqual(self.spool.status(), {})

    def test_parent_binding_store_is_owner_private_and_rejects_invalid_url(self):
        from local_agent.host_ops.capabilities.local.browser.chat_bridge_spool import (
            save_parent_url, validate_parent_url,
        )
        private = Path(self.directory.name) / "private"
        private.mkdir(mode=0o700)
        parent = private / "bridge.parent.json"
        url = "https://chatgpt.com/c/6acbb714-b818-83ed-9c99-138c5bbbf7d9"
        save_parent_url(parent, url)
        self.assertEqual(json.loads(parent.read_text())["parent_url"], url)
        self.assertEqual(parent.stat().st_mode & 0o777, 0o600)
        for raw in ("https://evil.test/c/1", url + "?x=1", "http://chatgpt.com/c/x", ""):
            with self.assertRaises(ValueError):
                validate_parent_url(raw)
        private.chmod(0o755)
        with self.assertRaises(PermissionError):
            save_parent_url(parent, url)

    def test_authenticated_gateway_accepts_only_canonical_active_parent(self):
        secret = "Q" * 48
        service = ThreadingHTTPServer(("127.0.0.1", 0), make_bridge_handler(self.spool, secret))
        thread = threading.Thread(target=service.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 1)
        self.addCleanup(service.server_close)
        self.addCleanup(service.shutdown)
        def parent_post(url, key=None):
            client = http.client.HTTPConnection("127.0.0.1", service.server_port, timeout=3)
            headers = {"Content-Type": "application/json"}
            if key:
                headers["X-Local-Agent-Bridge-Key"] = key
            client.request("POST", "/v1/parent", json.dumps({"parent_url": url}), headers)
            reply = client.getresponse()
            status = reply.status
            reply.read()
            client.close()
            return status
        url = "https://chatgpt.com/c/6acbb714-b818-83ed-9c99-138c5bbbf7d9"
        self.assertEqual(parent_post(url), 403)
        self.assertEqual(parent_post(url + "?x=2", secret), 400)
        self.assertEqual(parent_post(url, secret), 200)
        self.assertEqual(json.loads(self.spool.path.with_suffix(".parent.json").read_text())["parent_url"], url)
        self.assertEqual(self.spool.status(), {})

    def test_result_identity_mismatch_is_not_delivered(self):
        child = self.dispatch["children"][0]
        path = github_first_results.result_path(self.dispatch, child)
        self.api.snapshots[self.api.head][path] = json.dumps({
            "schema_version": 1, "kind": "child_agent_result",
            "dispatch_id": self.dispatch["id"], "child_request_id": "forged",
            "spawn_transaction_id": child["spawn"]["transaction_id"],
            "status": "completed", "result_text": "forged result"
        })
        with self.assertRaises(ValueError):
            github_first_bridge.sync_indexed_private_dispatches(self.spool, api=self.api)
        self.assertIsNone(self.spool.take())

    def test_loopback_gateway_auth_one_shot_after_restart(self):
        secret = "L" * 48
        server = ThreadingHTTPServer(("127.0.0.1", 0), make_bridge_handler(self.spool, secret))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 1)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        def query(method, path, data=None, key=None):
            client = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=2)
            body = None if data is None else json.dumps(data)
            headers = {} if key is None else {"X-Local-Agent-Bridge-Key": key}
            if body is not None:
                headers["Content-Type"] = "application/json"
            client.request(method, path, body=body, headers=headers)
            reply = client.getresponse()
            content = json.loads(reply.read())
            result = reply.status, content
            client.close()
            return result
        github_first_bridge.admit_dispatch(self.dispatch["id"], self.spool, enabled=True, api=self.api)
        self.assertEqual(query("GET", "/v1/next")[0], 403)
        code, body = query("GET", "/v1/next", key=secret)
        self.assertEqual(code, 200)
        self.assertEqual(body["launch"]["dispatch_id"], self.dispatch["id"])
        item_id = body["launch"]["intent"]["transaction_id"]
        # Simulate shutdown / restart without losing durable unknown boundary.
        self.assertEqual(ChatBridgeSpool(self.spool.path).status()[item_id]["phase"], "submission_unknown")
        self.assertIsNotNone(query("GET", "/v1/next", key=secret)[1]["launch"])
        self.assertIsNone(query("GET", "/v1/next", key=secret)[1]["launch"])
        item = body["launch"]
        self.assertEqual(query("POST", "/v1/ack", data={
            "transaction_id": item["intent"]["transaction_id"],
            "child_url": "https://chatgpt.com/c/child-test"}, key=secret)[0], 200)


if __name__ == "__main__":
    unittest.main()
