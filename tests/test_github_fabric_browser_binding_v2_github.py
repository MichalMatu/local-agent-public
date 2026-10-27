"""Synthetic v2 binding CAS races; no private repository or Chrome effects."""

from __future__ import annotations

import json
import unittest

from local_agent.conversation import github_fabric_browser_binding_v2 as policy
from local_agent.conversation import github_fabric_browser_binding_v2_github as writer
from local_agent.conversation import github_fabric_effect_intents_github as effects_git
from local_agent.conversation import github_fabric_github as git
from local_agent.conversation import github_fabric_parent_ownership_github as ownership
from local_agent.conversation import github_fabric_private_github as private_git
from local_agent.conversation import github_fabric_private_live as live
from local_agent.conversation import github_fabric_private_publication as catalog
from tests.test_github_fabric_dispatch import admitted_children, operator_request
from tests.test_github_fabric_parent_ownership_github import ParentGitDataAPI


class BrowserBindingSyntheticCASTests(unittest.TestCase):
    def setUp(self):
        self.api = ParentGitDataAPI()
        workflows = json.dumps({"schema_version": 1, "workflow_ids": ["workflow-001"]})
        self.api.snapshots[self.api.head][catalog.WORKFLOWS_PATH] = workflows
        self.api.trees["b" * 40][catalog.WORKFLOWS_PATH] = workflows
        staged = live.stage_private_dispatch(
            operator_request(), admitted_children(),
            enabled=True, writer_authorized=True, api=self.api,
        )
        self.dispatch_id = staged.dispatch_id
        self.owner_id = "1" * 32
        self.parent = ownership.acquire_parent_candidate(
            self.dispatch_id, self.owner_id,
            enabled=True, writer_authorized=True, api=self.api,
        )
        self.dispatch = json.loads(
            self.api.snapshots[self.api.head][catalog.dispatch_path(self.dispatch_id)]
        )
        self.first = self.dispatch["children"][0]["request_id"]
        self.second = self.dispatch["children"][1]["request_id"]
        self.driver = "b" * 32

    def publish(self, child=None, **changes):
        kwargs = {
            "driver_session_id": self.driver, "enabled": True,
            "writer_authorized": True, "api": self.api,
        }
        kwargs.update(changes)
        return writer.publish_synthetic_browser_binding(
            self.dispatch_id, self.owner_id, 1, child or self.first, **kwargs,
        )

    def binding_path(self, child):
        return policy.binding_path(self.parent.parent_id, 1, child)

    def test_default_disabled_and_explicit_synthetic_api_required(self):
        n = len(self.api.operations)
        for kw in (
            {"api": self.api},
            {"enabled": True, "api": self.api},
            {"enabled": True, "writer_authorized": True},
        ):
            with self.subTest(kw=kw), self.assertRaisesRegex(
                PermissionError, "default-disabled"
            ):
                writer.publish_synthetic_browser_binding(
                    self.dispatch_id, self.owner_id, 1, self.first,
                    driver_session_id=self.driver, **kw,
                )
        self.assertEqual(len(self.api.operations), n)

    def test_atomic_binding_has_no_send_authority_and_origin_is_pre_cas(self):
        origin_head = self.api.head
        commits = len(self.api.commits)
        outcome = self.publish()
        self.assertEqual(outcome.status, "recorded_no_send")
        self.assertIs(outcome.browser_send_authorized, False)
        self.assertEqual(len(self.api.commits), commits + 1)
        record = json.loads(self.api.snapshots[self.api.head][self.binding_path(self.first)])
        self.assertEqual(record["binding_origin_head_sha"], origin_head)
        self.assertEqual(record["parent_owner_id"], self.owner_id)
        self.assertEqual(record["driver_session_id"], self.driver)
        self.assertIs(record["browser_send_authorized"], False)
        self.assertNotIn("bootstrap_text", str(record))
        current_owner = json.loads(
            self.api.snapshots[self.api.head][
                ownership.policy.path(self.parent.parent_id)
            ]
        )
        self.assertEqual(outcome.binding_digest, policy.binding_digest(
            record, current_owner, self.dispatch,
            expected_origin_head_sha=origin_head,
        ))
        writes = [
            operation for operation in self.api.operations
            if operation[0] == "PATCH" and operation[1] == private_git.REF_UPDATE_PATH
        ]
        self.assertTrue(all(item[2]["force"] is False for item in writes))
        n = len(self.api.commits)
        with self.assertRaises(writer.BrowserBindingWriteConflict):
            self.publish()
        self.assertEqual(len(self.api.commits), n)

    def test_unique_sibling_binding_and_cross_child_driver_reuse_denied(self):
        self.publish()
        n = len(self.api.commits)
        with self.assertRaisesRegex(writer.BrowserBindingWriteConflict, "driver"):
            self.publish(self.second)
        self.assertEqual(len(self.api.commits), n)
        outcome = self.publish(self.second, driver_session_id="c" * 32)
        self.assertEqual(outcome.status, "recorded_no_send")
        self.assertIs(outcome.browser_send_authorized, False)
        self.assertNotEqual(
            self.binding_path(self.first), self.binding_path(self.second),
        )

    def test_foreign_owner_epoch_child_and_driver_fail_before_write(self):
        n = len(self.api.commits)
        for owner, epoch, child, driver in (
            ("2" * 32, 1, self.first, self.driver),
            (self.owner_id, 2, self.first, self.driver),
            (self.owner_id, 1, "../escape", self.driver),
            (self.owner_id, 1, self.first, self.owner_id),
        ):
            with self.subTest(owner=owner, epoch=epoch, child=child, driver=driver):
                with self.assertRaises((PermissionError, ValueError)):
                    writer.publish_synthetic_browser_binding(
                        self.dispatch_id, owner, epoch, child,
                        driver_session_id=driver, enabled=True,
                        writer_authorized=True, api=self.api,
                    )
        self.assertEqual(len(self.api.commits), n)

    def test_lost_patch_ack_never_republishes_or_authorizes_send(self):
        self.api.fail_after_ref = True
        n = len(self.api.commits)
        result = self.publish()
        self.assertEqual(result.status, "converged_no_send")
        self.assertIs(result.browser_send_authorized, False)
        self.assertEqual(len(self.api.commits), n + 1)
        with self.assertRaises(writer.BrowserBindingWriteConflict):
            self.publish()
        self.assertEqual(len(self.api.commits), n + 1)

    def test_concurrent_unrelated_ref_update_fails_closed_without_retry(self):
        self.api.reject_next_ref = True
        n = len(self.api.commits)
        with self.assertRaises(writer.BrowserBindingWriteConflict):
            self.publish()
        self.assertEqual(len(self.api.commits), n + 1)
        self.assertNotIn(self.binding_path(self.first), self.api.snapshots[self.api.head])

    def test_lost_ack_with_unreadable_reconciliation_is_uncertain(self):
        class UnreadableAfterPatch:
            def __init__(self, wrapped):
                self.wrapped = wrapped
                self.after = False

            def request(self, method, path, body=None):
                if self.after and method == "GET":
                    raise git.GithubFabricHTTPError(503)
                try:
                    return self.wrapped.request(method, path, body)
                except git.GithubFabricTransportError:
                    self.after = True
                    raise

        self.api.fail_after_ref = True
        with self.assertRaises(writer.BrowserBindingWriteUncertain):
            self.publish(api=UnreadableAfterPatch(self.api))
        self.assertIn(self.binding_path(self.first), self.api.snapshots[self.api.head])

    def test_binding_after_effect_arming_is_denied(self):
        effects_git.record_candidate_send_intent(
            self.dispatch_id, self.owner_id, 1, self.first,
            enabled=True, writer_authorized=True, api=self.api,
        )
        n = len(self.api.commits)
        with self.assertRaisesRegex(PermissionError, "cannot follow"):
            self.publish()
        self.assertEqual(len(self.api.commits), n)


if __name__ == "__main__":
    unittest.main()
