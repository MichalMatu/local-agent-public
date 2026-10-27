"""Pure candidate v2 identity/binding/receipt tests; no browser or GitHub IO."""
from __future__ import annotations

import copy
import unittest

from local_agent.conversation import github_fabric_browser_binding_v2 as v2
from local_agent.conversation import github_fabric_parent_ownership as parents
from tests.test_github_fabric_receipt_aggregation import dispatch

HEAD = "a" * 40
BROWSER = "b" * 32
URL = "https://chatgpt.com/c/6ac6840c-eaec-83ed-bde2-6971e9ffa221"


class BrowserIdentityBindingV2PolicyTests(unittest.TestCase):
    def setUp(self):
        self.dispatch = dispatch()
        _, self.parent = parents.propose_acquisition(self.dispatch, "c" * 32)
        self.child = self.dispatch["children"][0]
        self.binding = self.propose()

    def propose(self, **changes):
        opts = {"driver_session_id": BROWSER, "source_head_sha": HEAD}
        opts.update(changes)
        return v2.propose_binding(
            self.parent, self.dispatch, self.child["request_id"], **opts
        )

    def validate(self, record=None, *, parent=None, dispatch_value=None, head=HEAD):
        return v2.validate_binding(
            self.binding if record is None else record,
            self.parent if parent is None else parent,
            self.dispatch if dispatch_value is None else dispatch_value,
            expected_origin_head_sha=head,
        )

    def digest(self):
        return v2.binding_digest(
            self.binding, self.parent, self.dispatch,
            expected_origin_head_sha=HEAD,
        )

    def claim(self):
        return {
            "schema_version": 2, "kind": "browser_child_claim_v2",
            "dispatch_id": self.binding["dispatch_id"],
            "child_request_id": self.binding["child_request_id"],
            "spawn_transaction_id": self.binding["spawn_transaction_id"],
            "parent_id": self.binding["parent_id"],
            "parent_owner_id": self.binding["parent_owner_id"],
            "fence_epoch": self.binding["fence_epoch"],
            "effect_id": self.binding["effect_id"],
            "driver_session_id": self.binding["driver_session_id"],
            "binding_digest": self.digest(),
        }

    def ack(self):
        return {
            "schema_version": 2, "kind": "browser_spawn_ack_v2",
            "dispatch_id": self.binding["dispatch_id"],
            "child_request_id": self.binding["child_request_id"],
            "spawn_transaction_id": self.binding["spawn_transaction_id"],
            "binding_digest": self.digest(),
            "bootstrap_digest": self.child["spawn"]["bootstrap_digest"],
            "child_conversation_url": URL,
        }

    def result(self):
        return {
            "schema_version": 2, "kind": "browser_terminal_result_v2",
            "dispatch_id": self.binding["dispatch_id"],
            "child_request_id": self.binding["child_request_id"],
            "spawn_transaction_id": self.binding["spawn_transaction_id"],
            "binding_digest": self.digest(),
            "child_conversation_url": URL,
            "assistant_identity": "assistant-opaque-1",
            "assistant_text": "PRIVATE ANSWER MUST NOT BE PROJECTED",
        }

    def receipt(self, kind, record):
        return v2.validate_receipt(
            kind, record, self.binding, self.parent, self.dispatch,
            expected_origin_head_sha=HEAD,
        )

    def project(self, data):
        return v2.receipt_projection(
            self.parent, self.dispatch, self.binding, data,
            expected_origin_head_sha=HEAD,
        )

    def test_valid_binding_has_no_send_authority_and_is_deterministic(self):
        self.assertEqual(self.validate(), self.binding)
        self.assertEqual(self.propose(), self.binding)
        self.assertFalse(self.binding["browser_send_authorized"])
        self.assertNotIn("bootstrap_text", repr(self.binding))
        self.assertNotIn("assistant_text", repr(self.binding))
        self.assertRegex(self.digest(), r"^sha256:[a-f0-9]{64}$")

    def test_distinct_immutable_sibling_binding_and_effect(self):
        sibling = self.dispatch["children"][1]
        second = v2.propose_binding(
            self.parent, self.dispatch, sibling["request_id"],
            driver_session_id="d" * 32, source_head_sha=HEAD,
        )
        self.assertNotEqual(second["effect_id"], self.binding["effect_id"])
        self.assertNotEqual(second["child_request_id"], self.binding["child_request_id"])
        self.assertNotEqual(
            v2.binding_path(self.parent["id"], 1, sibling["request_id"]),
            v2.binding_path(self.parent["id"], 1, self.child["request_id"]),
        )

    def test_missing_or_wrong_child_never_binds(self):
        for child in ("missing-child", "../evil", "x/../../evil", ""):
            with self.subTest(child=child), self.assertRaises((PermissionError, ValueError)):
                v2.propose_binding(
                    self.parent, self.dispatch, child,
                    driver_session_id=BROWSER, source_head_sha=HEAD,
                )

    def test_child_and_parent_path_reject_escape_and_bool_epoch(self):
        for value in (0, 33, True, "1", -1):
            with self.subTest(epoch=value), self.assertRaises(ValueError):
                v2.binding_path(self.parent["id"], value, "child-1")
        for child in ("../wrong", "bad/name", ".", "", "x" * 121):
            with self.subTest(child=child), self.assertRaises(ValueError):
                v2.binding_path(self.parent["id"], 1, child)
        with self.assertRaises(ValueError):
            v2.binding_path("../../outside", 1, "child-1")
        self.assertTrue(v2.binding_path(self.parent["id"], 1, "child-1").endswith("/0001/child-1.json"))

    def test_receipt_paths_are_new_only_and_strict(self):
        path = v2.receipt_path(self.dispatch["id"], self.child["request_id"], "claim")
        self.assertIn("/receipts_v2/", path)
        for kind in ("../claim", "send", "", 1):
            with self.subTest(kind=kind), self.assertRaises(ValueError):
                v2.receipt_path(self.dispatch["id"], self.child["request_id"], kind)
        with self.assertRaises(ValueError):
            v2.receipt_path(self.dispatch["id"], "../evil", "ack")

    def test_completed_parent_can_read_existing_binding_but_not_propose_new_one(self):
        completed = {**self.parent, "phase": "completed", "completion": "verified"}
        self.assertEqual(self.validate(parent=completed), self.binding)
        validated = v2.validate_receipt(
            "result", self.result(), self.binding, completed, self.dispatch,
            expected_origin_head_sha=HEAD,
        )
        self.assertEqual(validated, self.result())
        projected = v2.receipt_projection(
            completed, self.dispatch, self.binding,
            {"claim": self.claim(), "ack": self.ack(), "result": self.result()},
            expected_origin_head_sha=HEAD,
        )
        self.assertEqual(projected["state"], "completed_evidence_only")
        self.assertFalse(projected["browser_send_authorized"])
        with self.assertRaises(PermissionError):
            v2.propose_binding(
                completed, self.dispatch, self.child["request_id"],
                driver_session_id=BROWSER, source_head_sha=HEAD,
            )

    def test_frozen_parent_cannot_propose_or_use_completion_projection(self):
        frozen = {**self.parent, "phase": "unknown_frozen", "completion": "unresolved"}
        with self.assertRaises(PermissionError):
            self.validate(parent=frozen)
        with self.assertRaises(PermissionError):
            v2.propose_binding(
                frozen, self.dispatch, self.child["request_id"],
                driver_session_id=BROWSER, source_head_sha=HEAD,
            )

    def test_wrong_parent_dispatch_or_url_is_rejected(self):
        wrong = {**self.parent, "dispatch_id": "fabric-" + "f" * 32}
        with self.assertRaises(PermissionError):
            self.validate(parent=wrong)
        wrong_url = {**self.parent, "parent_conversation_url":
                     "https://chatgpt.com/c/different-conversation"}
        with self.assertRaises(ValueError):
            self.validate(parent=wrong_url)

    def test_stale_wrong_or_missing_origin_sha_rejected(self):
        for sha in ("b" * 40, "A" * 40, "a" * 39, None, True):
            with self.subTest(sha=sha), self.assertRaises(ValueError):
                self.validate(head=sha)
        with self.assertRaises(TypeError):
            v2.validate_binding(self.binding, self.parent, self.dispatch)

    def test_driver_cannot_be_parent_owner_or_malformed(self):
        for driver in (self.parent["owner_id"], "B" * 32, "b" * 31, None, True, "../wrong"):
            with self.subTest(driver=driver), self.assertRaises(ValueError):
                self.propose(driver_session_id=driver)

    def test_forged_owner_epoch_effect_dispatch_and_transaction_rejected(self):
        samples = {
            "parent_id": "parent-" + "f" * 32,
            "fence_epoch": 2,
            "parent_owner_id": "e" * 32,
            "dispatch_id": "fabric-" + "f" * 32,
            "child_request_id": self.dispatch["children"][1]["request_id"],
            "spawn_transaction_id": "spawn-" + "a" * 64,
            "effect_id": "effect-" + "f" * 32,
            "driver_session_id": "wrong",
            "binding_origin_head_sha": "b" * 40,
        }
        for field, value in samples.items():
            with self.subTest(field=field), self.assertRaises((ValueError, PermissionError)):
                self.validate({**self.binding, field: value})

    def test_strict_schema_extra_missing_wrong_version_and_send_true(self):
        samples = [
            {**self.binding, "token": "secret"},
            {k: v for k, v in self.binding.items() if k != "effect_id"},
            {**self.binding, "schema_version": 1},
            {**self.binding, "schema_version": True},
            {**self.binding, "browser_send_authorized": True},
            {**self.binding, "browser_send_authorized": 0},
        ]
        for record in samples:
            with self.subTest(record=record), self.assertRaises(ValueError):
                self.validate(record)

    def test_binding_digest_tamper_changes(self):
        changed = {**self.binding, "driver_session_id": "e" * 32}
        self.assertNotEqual(
            self.digest(), v2.binding_digest(
                changed, self.parent, self.dispatch, expected_origin_head_sha=HEAD
            )
        )

    def test_v2_claim_is_bound_to_parent_and_driver(self):
        self.assertEqual(self.receipt("claim", self.claim()), self.claim())
        for field, value in (
            ("parent_owner_id", "a" * 32),
            ("driver_session_id", "f" * 32),
            ("fence_epoch", 2),
            ("effect_id", "effect-" + "0" * 32),
            ("binding_digest", "sha256:" + "0" * 64),
        ):
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.receipt("claim", {**self.claim(), field: value})

    def test_v1_claim_and_unsupported_receipt_type_fail(self):
        old = {**self.claim(), "schema_version": 1, "kind": "browser_child_claim"}
        with self.assertRaises(ValueError):
            self.receipt("claim", old)
        with self.assertRaises(ValueError):
            self.receipt("send", self.claim())

    def test_nonstring_and_unhashable_receipt_kind_fails_closed(self):
        for bad_kind in ([], {}, 1, None, True):
            with self.subTest(kind=bad_kind):
                with self.assertRaises(ValueError):
                    v2.receipt_path(
                        self.dispatch["id"], self.child["request_id"], bad_kind
                    )
                with self.assertRaises(ValueError):
                    self.receipt(bad_kind, self.claim())

    def test_ack_checks_pinned_bootstrap_and_url(self):
        self.assertEqual(self.receipt("ack", self.ack()), self.ack())
        for field, value in (
            ("bootstrap_digest", "sha256:" + "0" * 64),
            ("child_conversation_url", "https://evil.invalid/c/x"),
            ("binding_digest", "sha256:" + "a" * 64),
        ):
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.receipt("ack", {**self.ack(), field: value})

    def test_terminal_receipt_rejects_empty_private_result(self):
        self.assertEqual(self.receipt("result", self.result()), self.result())
        for field, value in (
            ("assistant_text", ""),
            ("assistant_text", " " * 10),
            ("assistant_text", "a" * 6001),
            ("assistant_identity", ""),
            ("assistant_identity", "x" * 257),
            ("child_conversation_url", "bad"),
        ):
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.receipt("result", {**self.result(), field: value})

    def test_any_receipt_extra_fields_or_wrong_identity_rejected(self):
        for kind, record in (
            ("claim", self.claim()), ("ack", self.ack()), ("result", self.result())
        ):
            with self.subTest(kind=kind), self.assertRaises(ValueError):
                self.receipt(kind, {**record, "read_token": "private"})
            with self.subTest(kind=kind), self.assertRaises(ValueError):
                self.receipt(kind, {**record, "child_request_id": "other-child"})
            with self.subTest(kind=kind), self.assertRaises(ValueError):
                self.receipt(kind, {**record, "binding_digest": "invalid"})

    def test_empty_receipts_do_not_prove_no_send(self):
        p = self.project({})
        self.assertEqual(p["state"], "pending")
        self.assertFalse(p["browser_send_authorized"])

    def test_claim_only_is_unknown_and_never_replayed(self):
        p = self.project({"claim": self.claim()})
        self.assertEqual(p["state"], "claim_only_unknown")
        self.assertTrue(p["requires_reconciliation"])
        self.assertFalse(p["browser_send_authorized"])

    def test_orphan_ack_and_result_are_not_completion(self):
        ack = self.project({"ack": self.ack()})
        result = self.project({"result": self.result()})
        self.assertEqual(ack["state"], "ack_without_claim")
        self.assertEqual(result["state"], "result_without_ack")
        self.assertTrue(ack["requires_reconciliation"])
        self.assertTrue(result["requires_reconciliation"])

    def test_acknowledged_is_not_completed(self):
        p = self.project({"claim": self.claim(), "ack": self.ack()})
        self.assertEqual(p["state"], "acknowledged")
        self.assertFalse(p["browser_send_authorized"])

    def test_result_with_wrong_child_url_requires_reconciliation(self):
        changed = {**self.result(), "child_conversation_url":
                   "https://chatgpt.com/c/another-child-chat"}
        p = self.project({"claim": self.claim(), "ack": self.ack(), "result": changed})
        self.assertEqual(p["state"], "child_url_mismatch")
        self.assertTrue(p["requires_reconciliation"])

    def test_complete_immutable_receipts_are_evidence_not_send_permission(self):
        p = self.project({
            "claim": self.claim(), "ack": self.ack(), "result": self.result(),
        })
        self.assertEqual(p["state"], "completed_evidence_only")
        self.assertFalse(p["browser_send_authorized"])
        self.assertFalse(p["requires_reconciliation"])
        self.assertNotIn("PRIVATE ANSWER", repr(p))
        self.assertNotIn("assistant_text", repr(p))
        self.assertNotIn("driver_session_id", repr(p))

    def test_extra_kind_and_mixed_v1_record_never_project(self):
        with self.assertRaises(ValueError):
            self.project({"claim": self.claim(), "v1_claim": {}})
        with self.assertRaises(ValueError):
            self.project({"claim": {**self.claim(), "schema_version": 1}})

    def test_original_inputs_unchanged_under_projection(self):
        receipts = {"claim": self.claim(), "ack": self.ack(), "result": self.result()}
        original = copy.deepcopy(receipts)
        self.project(receipts)
        self.assertEqual(receipts, original)


if __name__ == "__main__":
    unittest.main()
