"""Offline, deterministic shadow-only owner pairing protocol negatives.

These tests do not provision credentials, write operator-control, or run a
task. A structural match is deliberately NOT owner proof or permission.
"""
from __future__ import annotations

import unittest
from copy import deepcopy
from dataclasses import FrozenInstanceError

from local_agent.repository.host_routing import HostIdentity
from local_agent.runtime.task_contract import task_digest
from local_agent.supervisor.host_remote_grants import require_exact_host_command_grant
from local_agent.supervisor.owner_pairing_shadow import (
    PairingShadowInspection, canonical_owner_intent_bytes,
    inspect_owner_pairing_shadow, parse_owner_intent,
)


class OwnerPairingShadowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.identity = HostIdentity(
            "mac-test-host", "b0e94f93-478f-4fc6-abed-159d3702362f",
        )
        self.task = {
            "version": 1, "id": "owner-shadow-01",
            "target_host_id": self.identity.host_id,
            "target_installation_id": self.identity.installation_id,
            "mode": "commands", "commands": ["echo offline"],
            "resources": [],
        }
        self.binding = {
            "version": 1,
            "pairing_id": "98902923-c837-44d0-9843-dde3a6e7bd57",
            "owner_key_sha256": "b" * 64,
            "epoch": 3,
            "host_id": self.identity.host_id,
            "installation_id": self.identity.installation_id,
            "enabled": True,
            "expires_at": 2000,
            "revocation_checked_at": 900,
        }
        self.intent = {
            "version": 1,
            "pairing_id": self.binding["pairing_id"],
            "owner_key_sha256": self.binding["owner_key_sha256"],
            "epoch": 3,
            "task_id": self.task["id"],
            "task_digest": task_digest(self.task),
            "target_host_id": self.identity.host_id,
            "target_installation_id": self.identity.installation_id,
            "challenge": "ab" * 16,
        }

    def inspect(self, *, binding=None, intent=None, task=None, now=1000,
                age=150, ids=()) -> object:
        return inspect_owner_pairing_shadow(
            local_binding=self.binding if binding is None else binding,
            raw_intent=canonical_owner_intent_bytes(
                self.intent if intent is None else intent,
            ).split(b"\0", 1)[1],
            task=self.task if task is None else task,
            local_identity=self.identity, now_seconds=now,
            max_revocation_age_seconds=age, durable_task_ids=ids,
        )

    def test_structural_match_is_never_execution_permission(self) -> None:
        result = self.inspect()
        self.assertTrue(result.candidate_matches)
        self.assertEqual(result.reason, "structural_match_only_unsigned")
        self.assertEqual(result.execution_authority, "not_asserted")
        with self.assertRaises(TypeError):
            PairingShadowInspection(
                candidate_matches=True, reason="untrusted",
                execution_authority="approved",
            )
        with self.assertRaises((AttributeError, FrozenInstanceError, TypeError)):
            result.execution_authority = "approved"
        self.assertEqual(result.execution_authority, "not_asserted")
        with self.assertRaisesRegex(ValueError, "not approved"):
            require_exact_host_command_grant(
                identity=self.identity, task_id=self.task["id"],
                task_digest=task_digest(self.task), grants=(),
            )
        # Even a forged Git writer copying the entire unsigned intent can
        # only achieve a structural match, never an admission receipt.
        self.assertEqual(self.inspect().execution_authority, "not_asserted")

    def test_rejects_wrong_host_or_installation_even_with_matching_digest(self) -> None:
        for key, changed in (("target_host_id", "other-test-host"),
                             ("target_installation_id",
                              "e1552f5c-942c-4399-a06f-5b79f7832482")):
            altered = {**self.intent, key: changed}
            self.assertEqual(self.inspect(intent=altered).reason, "installation_mismatch")
        bad_binding = {**self.binding, "host_id": "other-test-host"}
        self.assertEqual(self.inspect(binding=bad_binding).reason, "installation_mismatch")
        bad_task = {**self.task, "target_host_id": "other-test-host"}
        self.assertEqual(self.inspect(task=bad_task).reason, "task_target_mismatch")

    def test_tampered_task_digest_or_task_id_does_not_match(self) -> None:
        changed = {**self.task, "commands": ["echo different"]}
        self.assertEqual(self.inspect(task=changed).reason, "task_digest_mismatch")
        mutated = {**self.intent, "task_digest": "c" * 64}
        self.assertEqual(self.inspect(intent=mutated).reason, "task_digest_mismatch")
        other = {**self.task, "id": "other-task"}
        self.assertEqual(self.inspect(task=other).reason, "task_target_mismatch")

    def test_epoch_pairing_id_and_key_rotation_do_not_silently_authorize(self) -> None:
        for key, value in (("epoch", 4),
                           ("pairing_id", "847c98ba-9979-4b55-9b73-292418d3a14e"),
                           ("owner_key_sha256", "a" * 64)):
            altered = {**self.intent, key: value}
            self.assertEqual(self.inspect(intent=altered).reason, "owner_binding_mismatch")
        rotated = {**self.binding, "epoch": 4}
        self.assertEqual(self.inspect(binding=rotated).reason, "owner_binding_mismatch")

    def test_revoked_expired_stale_or_future_revocation_check_fails_closed(self) -> None:
        disabled = {**self.binding, "enabled": False}
        self.assertEqual(self.inspect(binding=disabled).reason, "locally_disabled")
        expired = {**self.binding, "expires_at": 1000}
        self.assertEqual(self.inspect(binding=expired).reason, "locally_expired")
        self.assertEqual(self.inspect(now=1100).reason, "revocation_freshness_unverified")
        future = {**self.binding, "revocation_checked_at": 1001}
        self.assertEqual(self.inspect(binding=future).reason,
                         "revocation_freshness_unverified")
        with self.assertRaises(ValueError):
            self.inspect(age=0)

    def test_known_task_id_is_never_replayed_in_shadow(self) -> None:
        result = self.inspect(ids=frozenset({self.task["id"]}))
        self.assertFalse(result.candidate_matches)
        self.assertEqual(result.reason, "already_seen_task_id")
        self.assertEqual(result.execution_authority, "not_asserted")

    def test_parser_rejects_duplicate_unknown_noncanonical_and_nonfinite(self) -> None:
        valid = canonical_owner_intent_bytes(self.intent).split(b"\0", 1)[1]
        self.assertEqual(parse_owner_intent(valid), self.intent)
        invalid = [
            b" " + valid,
            valid + b"\n",
            b'{"version":1,"version":1}',
            b'{"version":NaN}',
            b"{}",
            valid.replace(b'"version":1', b'"version":true', 1),
        ]
        for raw in invalid:
            with self.subTest(raw=raw[:42]), self.assertRaises(ValueError):
                parse_owner_intent(raw)
        with self.assertRaises(ValueError):
            parse_owner_intent(b"!" * 4097)
        with self.assertRaises(ValueError):
            canonical_owner_intent_bytes({**self.intent, "signature": "fake"})

    def test_rejects_ambiguous_local_binding_and_unbounded_context(self) -> None:
        for key, value in (("epoch", True), ("enabled", 1),
                           ("expires_at", 0.1), ("revocation_checked_at", -5)):
            local = {**self.binding, key: value}
            with self.subTest(key=key), self.assertRaises(ValueError):
                self.inspect(binding=local)
        local = deepcopy(self.binding)
        local["untrusted_git_grant"] = True
        with self.assertRaises(ValueError):
            self.inspect(binding=local)
        with self.assertRaises(ValueError):
            self.inspect(ids="untrusted_unbounded_history")
        # Supplying a forged binding cannot make this a credential verifier.
        self.assertEqual(self.inspect().execution_authority, "not_asserted")


if __name__ == "__main__":
    unittest.main()
