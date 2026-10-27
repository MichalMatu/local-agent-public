"""Read-only pinned private summary tests using a local fake GitHub API."""

from __future__ import annotations

import copy
import unittest
from unittest.mock import patch

from local_agent.conversation import github_fabric_private_publication as catalog
from local_agent.conversation.github_fabric_private_summary import (
    RECEIPT_ROOT,
    read_private_dispatch_summary,
)
from tests.test_github_fabric_receipt_aggregation import (
    URL_A,
    URL_B,
    dispatch,
    evidence,
)


HEAD = "7" * 40


class PrivateSummaryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.batch = dispatch()
        self.reads: list[tuple[str, str, int]] = []
        self.files: dict[str, dict] = {
            catalog.CATALOG_PATH: {"schema_version": 1, "project_ids": ["local-agent"]},
            catalog.WORKFLOWS_PATH: {"schema_version": 1, "workflow_ids": ["workflow-001"]},
            catalog.INDEX_PATH: {"schema_version": 1, "dispatch_ids": [self.batch["id"]]},
            catalog.dispatch_path(self.batch["id"]): copy.deepcopy(self.batch),
        }

    def fake_read(self, _api, path, head, *, max_bytes):
        self.assertEqual(head, HEAD, "all evidence must share one pinned Git commit")
        self.assertLessEqual(max_bytes, 128 * 1024)
        self.reads.append((path, head, max_bytes))
        return copy.deepcopy(self.files.get(path))

    def fake_api(self):
        class API:
            def request(self, method, path):
                if method != "GET" or path != "/git/ref/heads/fabric-data":
                    raise AssertionError("unexpected API call")
                return {
                    "ref": "refs/heads/fabric-data",
                    "object": {"type": "commit", "sha": HEAD},
                }
        return API()

    def summary(self):
        with patch(
            "local_agent.conversation.github_fabric_private_summary.git._read_json_at_commit",
            side_effect=self.fake_read,
        ):
            return read_private_dispatch_summary(
                self.batch["id"], enabled=True, api=self.fake_api()
            )

    def put(self, index, receipt_kind, value):
        child_id = self.batch["children"][index]["request_id"]
        self.files[
            f"{RECEIPT_ROOT}{self.batch['id']}/{receipt_kind}/{child_id}.json"
        ] = value[receipt_kind]

    def test_two_children_completed_and_pinned_read_no_private_leak(self) -> None:
        for index, url in enumerate((URL_A, URL_B)):
            record = evidence(self.batch, index, url=url)
            for kind in ("claim", "ack", "result"):
                self.put(index, kind, record)
        summary = self.summary()
        self.assertEqual(summary["source_head_sha"], HEAD)
        self.assertEqual(summary["state"], "completed")
        self.assertEqual(summary["completed"], 2)
        self.assertEqual(len(self.reads), 10)
        self.assertNotIn("SENSITIVE", repr(summary))
        self.assertNotIn("bootstrap_text", repr(summary))
        self.assertNotIn("assistant_text", repr(summary))

    def test_claim_only_remains_unknown_and_partial_result_independent(self) -> None:
        first = evidence(self.batch, 0, url=URL_A)
        second = evidence(self.batch, 1, url=URL_B)
        for kind in ("claim", "ack", "result"):
            self.put(0, kind, first)
        self.put(1, "claim", second)
        result = self.summary()
        self.assertEqual(result["state"], "partial")
        self.assertTrue(result["requires_reconciliation"])
        self.assertEqual(
            [child["state"] for child in result["children"]],
            ["completed", "claim_only_unknown"],
        )

    def test_missing_or_unindexed_dispatch_fails_closed(self) -> None:
        self.files.pop(catalog.INDEX_PATH)
        with self.assertRaisesRegex(PermissionError, "not indexed"):
            self.summary()
        self.files[catalog.INDEX_PATH] = {
            "schema_version": 1, "dispatch_ids": [self.batch["id"]]
        }
        self.files.pop(catalog.dispatch_path(self.batch["id"]))
        with self.assertRaisesRegex(ValueError, "missing"):
            self.summary()

    def test_credential_and_flag_admission_before_network(self) -> None:
        with self.assertRaisesRegex(PermissionError, "default-disabled"):
            read_private_dispatch_summary(self.batch["id"], api=self.fake_api())
        with self.assertRaisesRegex(ValueError, "dispatch path invalid"):
            read_private_dispatch_summary("../unsafe", enabled=True, api=self.fake_api())


if __name__ == "__main__":
    unittest.main()
