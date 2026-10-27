"""Isolated GitHub Data CAS tests for candidate parent ownership; zero Chrome effects."""

from __future__ import annotations

import copy
import json
import unittest

from local_agent.conversation import github_fabric_github as git
from local_agent.conversation import github_fabric_private_github as private_git
from local_agent.conversation import github_fabric_private_live as live
from local_agent.conversation import github_fabric_private_publication as catalog
from local_agent.conversation import github_fabric_parent_ownership as policy
from local_agent.conversation import github_fabric_parent_ownership_github as writer
from tests.test_github_fabric_dispatch import admitted_children, operator_request
from tests.test_github_fabric_private_github import PrivateGitDataAPI
from tests.test_github_fabric_receipt_aggregation import evidence, URL_A, URL_B


class ParentGitDataAPI(PrivateGitDataAPI):
    """Commit-pinned private API supporting atomic updates of up to four paths."""

    def request(self, method, path, body=None):
        if method == "POST" and path == "/git/trees":
            self.operations.append((method, path, copy.deepcopy(body)))
            entries = body["tree"]
            if not 1 <= len(entries) <= 4:
                raise AssertionError("unsafe atomic tree width")
            if len({entry["path"] for entry in entries}) != len(entries):
                raise AssertionError("duplicated paths")
            sha = self.new_sha()
            contents = copy.deepcopy(self.trees[body["base_tree"]])
            for entry in entries:
                if entry["mode"] != "100644" or entry["type"] != "blob":
                    raise AssertionError("unsafe blob mode")
                contents[entry["path"]] = self.blobs[entry["sha"]]
            self.trees[sha] = contents
            return {"sha": sha}
        return super().request(method, path, body)


class ParentCandidateCASAPITests(unittest.TestCase):
    def setUp(self):
        self.api = ParentGitDataAPI()
        workflows = json.dumps({"schema_version": 1, "workflow_ids": ["workflow-001"]})
        self.api.snapshots[self.api.head][catalog.WORKFLOWS_PATH] = workflows
        self.api.trees["b" * 40][catalog.WORKFLOWS_PATH] = workflows
        self.operator = operator_request()
        self.children = admitted_children()
        self.dispatch_id = self.stage(self.operator, self.children)
        self.owner = "1" * 32
        self.other = "2" * 32
        self.initial_commits = len(self.api.commits)

    def stage(self, request, children):
        result = live.stage_private_dispatch(
            request, children, enabled=True, writer_authorized=True, api=self.api
        )
        self.assertIn(result.status, {"converged", "replay"})
        return result.dispatch_id

    def stage_competitor(self, suffix=2):
        operator = copy.deepcopy(self.operator)
        children = copy.deepcopy(self.children)
        operator["id"] = f"operator-request-{suffix:03d}"
        for index, child in enumerate(children):
            new_id = child["id"] + f"-{suffix}"
            child["id"] = new_id
            operator["children"][index]["request_id"] = new_id
        return self.stage(operator, children)

    def acquire(self, dispatch_id=None, owner=None, **opts):
        params = {
            "enabled": True, "writer_authorized": True, "api": self.api,
        }
        params.update(opts)
        return writer.acquire_parent_candidate(
            dispatch_id or self.dispatch_id,
            owner or self.owner,
            **params,
        )

    def change(self, action, dispatch_id=None, owner=None, epoch=1, **opts):
        params = {
            "enabled": True, "writer_authorized": True, "api": self.api,
        }
        params.update(opts)
        return writer.change_parent_candidate(
            dispatch_id or self.dispatch_id,
            owner or self.owner, epoch, action, **params,
        )

    def records(self):
        return self.api.snapshots[self.api.head]

    def add_child_receipts(self, dispatch_id=None, owner_id=None):
        dispatch = json.loads(
            self.records()[catalog.dispatch_path(dispatch_id or self.dispatch_id)]
        )
        for i, url in enumerate((URL_A, URL_B)):
            child = dispatch["children"][i]
            receipt = evidence(dispatch, i, url=url)
            receipt["claim"]["owner_id"] = (
                self.owner if owner_id is None else owner_id
            )
            # We are simulating append-only private evidence at the current
            # head; this is not an actual GitHub commit or browser Send.
            for kind, value in receipt.items():
                path = (
                    f"projects/local-agent/workflows/workflow-001/receipts/"
                    f"{dispatch['id']}/{kind}/{child['request_id']}.json"
                )
                self.records()[path] = json.dumps(value)
                tree = self.api.commit_trees[self.api.head]
                self.api.trees[tree][path] = json.dumps(value)

    def test_disabled_before_api_and_non_executable_result(self):
        operations = len(self.api.operations)
        with self.assertRaisesRegex(PermissionError, "default-disabled"):
            writer.acquire_parent_candidate(self.dispatch_id, self.owner, api=self.api)
        with self.assertRaisesRegex(PermissionError, "default-disabled"):
            writer.acquire_parent_candidate(
                self.dispatch_id, self.owner, enabled=True, api=self.api
            )
        with self.assertRaisesRegex(PermissionError, "default-disabled"):
            writer.change_parent_candidate(
                self.dispatch_id, self.owner, 1, "complete", api=self.api
            )
        self.assertEqual(len(self.api.operations), operations)
        with self.assertRaisesRegex(ValueError, "dispatch path invalid"):
            self.acquire(dispatch_id="../untrusted")
        with self.assertRaisesRegex(ValueError, "state change invalid"):
            self.change("browser_send_authorized")
        self.assertEqual(writer.private_git.PRIVATE_REPOSITORY,
                         "example-owner/local-agent-fabric-private")

    def test_first_acquisition_is_atomic_replay_safe_and_never_authority(self):
        result = self.acquire()
        self.assertEqual(result.status, "created")
        self.assertEqual(result.fence_epoch, 1)
        self.assertIs(result.browser_send_authorized, False)
        identifier = result.parent_id
        self.assertEqual(len(self.api.commits), self.initial_commits + 1)
        self.assertEqual(
            json.loads(self.records()[writer.INDEX_PATH])["parent_ids"], [identifier]
        )
        self.assertEqual(
            json.loads(self.records()[writer.history_path(identifier)])["entries"],
            [{"epoch": 1, "dispatch_id": self.dispatch_id, "owner_id": self.owner}]
        )
        self.assertEqual(
            json.loads(self.records()[writer.epoch_path(identifier, 1)])["phase"],
            "active"
        )
        self.assertEqual(
            json.loads(self.records()[policy.path(identifier)])["phase"],
            "active"
        )
        last = list(self.api.commits.values())[-1]
        self.assertEqual(len(self.api.trees[last["tree"]]), len(self.records()))
        parent_tree_write = [
            call for call in self.api.operations
            if call[0] == "POST" and call[1] == "/git/trees"
        ][-1]
        self.assertEqual(
            {item["path"] for item in parent_tree_write[2]["tree"]},
            {writer.INDEX_PATH, policy.path(identifier),
             writer.history_path(identifier), writer.epoch_path(identifier, 1)}
        )
        ref_writes = [item for item in self.api.operations
                      if item[0] == "PATCH" and item[1] == private_git.REF_UPDATE_PATH]
        self.assertTrue(all(item[2]["force"] is False for item in ref_writes))
        self.assertEqual(self.acquire().status, "replay")
        self.assertEqual(len(self.api.commits), self.initial_commits + 1)

    def test_two_controllers_one_parent_and_unresolved_frozen_owner(self):
        competitor = self.stage_competitor()
        self.acquire()
        count = len(self.api.commits)
        with self.assertRaisesRegex(PermissionError, "already owned"):
            self.acquire(competitor, self.other)
        with self.assertRaisesRegex(PermissionError, "historic dispatch/owner replay"):
            self.acquire(self.dispatch_id, self.other)
        self.assertEqual(len(self.api.commits), count)
        frozen = self.change("freeze_unknown")
        self.assertEqual(frozen.status, "created")
        self.assertEqual(self.change("freeze_unknown").status, "replay")
        with self.assertRaisesRegex(PermissionError, "manual reconciliation"):
            self.acquire(competitor, self.other)
        with self.assertRaisesRegex(PermissionError, "completion identity mismatch"):
            self.change("complete")

    def test_missing_ack_result_never_closes_and_historic_replay_fails(self):
        competitor = self.stage_competitor()
        self.acquire()
        with self.assertRaisesRegex(PermissionError, "requires pinned all-child receipts"):
            self.change("complete")
        self.add_child_receipts()
        completed = self.change("complete")
        self.assertEqual(completed.status, "created")
        self.assertEqual(self.change("complete").status, "replay")
        self.assertEqual(
            json.loads(self.records()[writer.epoch_path(completed.parent_id, 1)])["phase"],
            "active", "epoch witness stays immutable after closure"
        )
        next_epoch = self.acquire(competitor, self.other)
        self.assertEqual(next_epoch.fence_epoch, 2)
        self.assertEqual(next_epoch.status, "created")
        self.assertFalse(next_epoch.browser_send_authorized)
        with self.assertRaisesRegex(PermissionError, "historic dispatch/owner replay"):
            self.acquire(self.dispatch_id, "3" * 32)
        with self.assertRaisesRegex(PermissionError, "historic dispatch/owner replay"):
            self.acquire(competitor, self.owner)
        self.assertEqual(
            len(json.loads(self.records()[
                writer.history_path(next_epoch.parent_id)
            ])["entries"]), 2
        )
        witness1 = json.loads(self.records()[writer.epoch_path(next_epoch.parent_id, 1)])
        witness2 = json.loads(self.records()[writer.epoch_path(next_epoch.parent_id, 2)])
        self.assertNotEqual(witness1["dispatch_id"], witness2["dispatch_id"])

    def test_existing_effect_ledger_blocks_epoch_rollover_without_archive(self):
        # A previous epoch's effect intent may have reached the browser.
        # Do not let an otherwise completed owner switch epochs while the
        # per-parent effects file still belongs to the earlier transaction.
        competitor = self.stage_competitor()
        first = self.acquire()
        self.add_child_receipts()
        self.change("complete")
        ledger_path = (
            f"projects/local-agent/parent_ownership/effects/"
            f"{first.parent_id}.json"
        )
        payload = json.dumps({"schema_version": 1, "effects": ["unknown"]})
        self.records()[ledger_path] = payload
        self.api.trees[self.api.commit_trees[self.api.head]][ledger_path] = payload
        before = len(self.api.commits)
        with self.assertRaisesRegex(PermissionError, "requires epoch archival"):
            self.acquire(competitor, self.other)
        self.assertEqual(len(self.api.commits), before)
        self.assertEqual(
            json.loads(self.records()[writer.history_path(first.parent_id)])
            ["entries"][-1]["epoch"], 1,
        )
        # Exact active-owner replay must also remain a no-write operation.
        # Do not change a completed predecessor merely to simplify takeover.
        self.assertEqual(
            json.loads(self.records()[policy.path(first.parent_id)])["phase"],
            "completed",
        )

    def test_archive_record_alone_never_releases_completed_parent_epoch(self):
        # A standalone archive-shaped record is neither an atomic migration
        # marker nor a Send-side lease. Without the original ledger, it must
        # still block acquisition until every driver uses one protocol.
        competitor = self.stage_competitor()
        first = self.acquire()
        self.add_child_receipts()
        self.change("complete")
        path = writer.effect_archives.archive_path(first.parent_id, 1)
        payload = json.dumps({
            "schema_version": 1,
            "kind": writer.effect_archives.ARCHIVE_KIND,
            "parent_id": first.parent_id,
            "browser_send_authorized": False,
        })
        self.assertNotIn(writer.effects.effect_path(first.parent_id), self.records())
        self.records()[path] = payload
        self.api.trees[self.api.commit_trees[self.api.head]][path] = payload
        before = len(self.api.commits)
        with self.assertRaisesRegex(PermissionError, "not a takeover permit"):
            self.acquire(competitor, self.other)
        self.assertEqual(len(self.api.commits), before)
        self.assertEqual(
            json.loads(self.records()[writer.history_path(first.parent_id)])
            ["entries"][-1]["epoch"], 1,
        )
        self.assertNotIn(writer.epoch_path(first.parent_id, 2), self.records())

    def test_orphan_epoch_archive_denies_first_parent_acquisition(self):
        # A missing ownership index is not proof that an earlier controller
        # never wrote an archive. An orphan record at epoch 7 must block the
        # very first proposed parent epoch with zero ref or CAS writes.
        identifier = policy.parent_id(self.operator["parent_conversation_url"])
        archive_path = writer.effect_archives.archive_path(identifier, 7)
        data = json.dumps({"schema_version": 1, "kind": "orphan_evidence"})
        self.records()[archive_path] = data
        self.api.trees[self.api.commit_trees[self.api.head]][archive_path] = data
        before = len(self.api.commits)
        with self.assertRaisesRegex(PermissionError, "not a takeover permit"):
            self.acquire()
        self.assertEqual(len(self.api.commits), before)
        self.assertNotIn(writer.history_path(identifier), self.records())
        self.assertNotIn(writer.epoch_path(identifier, 1), self.records())

    def test_unindexed_future_epoch_archive_denies_second_owner(self):
        # A forged or future key outside the current history is a conflict,
        # not a sign that only older numbered archives need checking.
        competitor = self.stage_competitor()
        first = self.acquire()
        self.add_child_receipts()
        self.change("complete")
        archive_path = writer.effect_archives.archive_path(first.parent_id, 32)
        data = json.dumps({"schema_version": 1, "kind": "future_evidence"})
        self.records()[archive_path] = data
        self.api.trees[self.api.commit_trees[self.api.head]][archive_path] = data
        before = len(self.api.commits)
        with self.assertRaisesRegex(PermissionError, "not a takeover permit"):
            self.acquire(competitor, self.other)
        self.assertEqual(len(self.api.commits), before)
        self.assertNotIn(writer.epoch_path(first.parent_id, 2), self.records())

    def test_archives_from_any_historic_epoch_block_later_takeover(self):
        # A completed epoch 2 must not hide an unreviewed archive belonging
        # to epoch 1 when a third owner attempts a new CAS acquisition.
        second = self.stage_competitor(2)
        third = self.stage_competitor(3)
        first_owner = self.acquire()
        self.add_child_receipts()
        self.change("complete")
        second_owner = self.acquire(second, self.other)
        self.assertEqual(second_owner.fence_epoch, 2)
        self.add_child_receipts(second, owner_id=self.other)
        self.change("complete", dispatch_id=second, owner=self.other, epoch=2)
        old_archive = writer.effect_archives.archive_path(first_owner.parent_id, 1)
        payload = json.dumps({
            "schema_version": 1,
            "kind": writer.effect_archives.ARCHIVE_KIND,
            "parent_id": first_owner.parent_id,
            "browser_send_authorized": False,
        })
        self.records()[old_archive] = payload
        self.api.trees[self.api.commit_trees[self.api.head]][old_archive] = payload
        before = len(self.api.commits)
        with self.assertRaisesRegex(PermissionError, "not a takeover permit"):
            self.acquire(third, "3" * 32)
        self.assertEqual(len(self.api.commits), before)
        self.assertEqual(
            len(json.loads(self.records()[
                writer.history_path(first_owner.parent_id)
            ])["entries"]), 2,
        )
        self.assertNotIn(writer.epoch_path(first_owner.parent_id, 3), self.records())

    def test_lost_ref_ack_converges_with_one_nonforce_write(self):
        self.api.fail_after_ref_number = self.api.ref_successes + 1
        result = self.acquire()
        self.assertEqual(result.status, "converged")
        self.assertEqual(len(self.api.commits), self.initial_commits + 1)
        self.assertEqual(self.acquire().status, "replay")
        updates = [item for item in self.api.operations
                   if item[0] == "PATCH"]
        self.assertEqual(len(updates), 2, "one staging ref and one candidate CAS")

    def test_non_fast_forward_conflict_has_no_retry(self):
        self.api.reject_next_ref = True
        with self.assertRaises(writer.ParentCandidateConflict):
            self.acquire()
        updates = [item for item in self.api.operations
                   if item[0] == "PATCH"]
        self.assertEqual(len(updates), 2, "never retry a rejected CAS automatically")
        self.assertEqual(len(self.records().get(writer.INDEX_PATH, "")), 0)

    def test_ambiguous_transport_and_unreadable_recovery_fails_closed(self):
        class LostAPI:
            def __init__(self, underlying):
                self.underlying = underlying
                self.lost = False

            def request(self, method, path, body=None):
                if method == "PATCH" and path == private_git.REF_UPDATE_PATH:
                    self.lost = True
                    raise git.GithubFabricTransportError("loss before update")
                if self.lost and method == "GET":
                    raise git.GithubFabricTransportError("reconciliation offline")
                return self.underlying.request(method, path, body)

        with self.assertRaises(writer.ParentCandidateWriteUncertain):
            self.acquire(api=LostAPI(self.api))
        self.assertEqual(len(self.api.commits), self.initial_commits + 1)
        self.assertEqual(self.api.ref_successes, 1)

    def test_index_history_witness_corruption_rejected_without_writes(self):
        self.acquire()
        identifier = policy.parent_id(self.operator["parent_conversation_url"])
        count = len(self.api.commits)
        snapshots = [
            (writer.history_path(identifier), {"schema_version": 1,
              "parent_id": identifier, "entries": []}),
            (writer.INDEX_PATH, {"schema_version": 1, "parent_ids": []}),
            (writer.epoch_path(identifier, 1), {"schema_version": 1,
              "forged": True}),
        ]
        for record_path, value in snapshots:
            previous = self.records()[record_path]
            self.records()[record_path] = json.dumps(value)
            with self.subTest(path=record_path):
                with self.assertRaises(ValueError):
                    self.acquire()
            self.records()[record_path] = previous
        self.assertEqual(len(self.api.commits), count)

    def test_foreign_claim_owner_cannot_complete_parent_epoch(self):
        self.acquire()
        self.add_child_receipts(owner_id=self.other)
        before = len(self.api.commits)
        with self.assertRaisesRegex(PermissionError, "requires pinned all-child receipts"):
            self.change("complete")
        self.assertEqual(len(self.api.commits), before)
        summary = writer._receipt_summary(
            self.api, writer._snapshot(self.api, self.dispatch_id)
        )
        self.assertTrue(summary["requires_reconciliation"])
        self.assertEqual(summary["state"], "invalid_evidence")
        self.assertEqual(
            [row["reason"] for row in summary["children"]],
            ["owner_id_conflict", "owner_id_conflict"],
        )
        self.assertEqual(
            json.loads(self.records()[policy.path(
                policy.parent_id(self.operator["parent_conversation_url"])
            )])["phase"], "active",
        )

    def test_pinned_receipt_tamper_refuses_completion(self):
        self.acquire()
        self.add_child_receipts()
        dispatch = json.loads(self.records()[catalog.dispatch_path(self.dispatch_id)])
        bad_path = (
            f"projects/local-agent/workflows/workflow-001/receipts/"
            f"{self.dispatch_id}/ack/{dispatch['children'][1]['request_id']}.json"
        )
        previous = self.records()[bad_path]
        receipt = json.loads(previous)
        receipt["bootstrap_digest"] = "sha256:" + "0" * 64
        self.records()[bad_path] = json.dumps(receipt)
        with self.assertRaisesRegex(PermissionError, "requires pinned all-child receipts"):
            self.change("complete")
        self.records()[bad_path] = previous
        self.assertEqual(self.change("complete").status, "created")


if __name__ == "__main__":
    unittest.main()
