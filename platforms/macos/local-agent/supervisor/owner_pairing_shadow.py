"""Offline-only candidate checks for a possible owner-paired host protocol.

This module is intentionally NOT connected to remote intake, operator state,
scheduler or executor. Syntactic eligibility is not cryptographic owner proof:
there is no approved key custody/ceremony/signature algorithm yet. Even a
perfectly matching candidate NEVER authorizes a task.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Collection, Mapping
from uuid import UUID

from local_agent.repository.host_routing import HostIdentity
from local_agent.runtime.task_contract import task_digest

_MAX_INTENT_BYTES = 4096
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_NONCE = re.compile(r"^[0-9a-f]{32,64}$")
_TASK = re.compile(r"^[A-Za-z0-9._-]{1,120}$")
_INTENT_FIELDS = frozenset({
    "version", "pairing_id", "owner_key_sha256", "epoch",
    "task_id", "task_digest", "target_host_id", "target_installation_id",
    "challenge",
})
_BINDING_FIELDS = frozenset({
    "version", "pairing_id", "owner_key_sha256", "epoch",
    "host_id", "installation_id", "enabled", "expires_at",
    "revocation_checked_at",
})


@dataclass(frozen=True, slots=True)
class PairingShadowInspection:
    """A check result that is NEVER admission, permission or signature proof."""

    candidate_matches: bool
    reason: str

    @property
    def execution_authority(self) -> str:
        """A shadow inspection has no configurable or assignable authority."""
        return "not_asserted"


def _unique_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    output: dict[str, Any] = {}
    for key, item in pairs:
        if key in output:
            raise ValueError("duplicate pairing intent JSON key")
        output[key] = item
    return output


def canonical_owner_intent_bytes(intent: Mapping[str, Any]) -> bytes:
    """Versioned draft signing preimage, NOT a signed task or execution grant."""
    if not isinstance(intent, dict) or set(intent) != _INTENT_FIELDS:
        raise ValueError("unsupported pairing intent fields")
    if type(intent["version"]) is not int or intent["version"] != 1:
        raise ValueError("unsupported pairing intent version")
    _pair_id(intent["pairing_id"])
    _key_sha(intent["owner_key_sha256"])
    _epoch(intent["epoch"])
    if (not isinstance(intent["task_id"], str)
            or _TASK.fullmatch(intent["task_id"]) is None
            or not isinstance(intent["task_digest"], str)
            or _SHA256.fullmatch(intent["task_digest"]) is None):
        raise ValueError("invalid pairing task ID or digest")
    HostIdentity(intent["target_host_id"], intent["target_installation_id"])
    challenge = intent["challenge"]
    if not isinstance(challenge, str) or _NONCE.fullmatch(challenge) is None:
        raise ValueError("pairing challenge must be 16-32 bytes of lowercase hex")
    return b"local-agent:owner-pairing-intent:v1\0" + json.dumps(
        intent, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def parse_owner_intent(raw: bytes) -> dict[str, Any]:
    """Reject duplicate/noncanonical encoding, unknown fields and oversized data."""
    if not isinstance(raw, bytes) or not 0 < len(raw) <= _MAX_INTENT_BYTES:
        raise ValueError("pairing intent bytes must be nonempty and bounded")
    try:
        record = json.loads(
            raw.decode("utf-8"), object_pairs_hook=_unique_pairs,
            parse_constant=lambda value: (_ for _ in ()).throw(
                ValueError("nonfinite pairing intent field")
            ),
        )
        canonical = canonical_owner_intent_bytes(record)
    except (UnicodeError, json.JSONDecodeError, TypeError, OverflowError) as exc:
        raise ValueError("invalid pairing intent JSON") from exc
    if raw != canonical.split(b"\0", 1)[1]:
        raise ValueError("pairing intent is not canonically encoded")
    return record


def _pair_id(value: object) -> None:
    if not isinstance(value, str):
        raise ValueError("pairing ID must be a canonical UUID")
    try:
        parsed = UUID(value)
    except (ValueError, AttributeError) as exc:
        raise ValueError("pairing ID must be a canonical UUID") from exc
    if str(parsed) != value:
        raise ValueError("pairing ID must be a lowercase canonical UUID")


def _key_sha(value: object) -> None:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise ValueError("owner key fingerprint must be SHA-256 lowercase hex")


def _epoch(value: object) -> None:
    if type(value) is not int or not 1 <= value <= 2**53:
        raise ValueError("pairing epoch must be a positive bounded integer")


def inspect_owner_pairing_shadow(
    *, local_binding: Mapping[str, Any], raw_intent: bytes,
    task: dict[str, Any], local_identity: HostIdentity,
    now_seconds: int, max_revocation_age_seconds: int,
    durable_task_ids: Collection[str],
) -> PairingShadowInspection:
    """Compare offline fixture snapshots only; never verify signatures or admit.

    Caller-supplied binding/freshness/history are NOT proven owner provenance.
    A match only means the candidate warrants later cryptographic design review.
    The existing exact-digest operator grant remains mandatory independently.
    """
    if (not isinstance(local_identity, HostIdentity)
            or not isinstance(local_binding, dict)
            or set(local_binding) != _BINDING_FIELDS):
        raise ValueError("local pairing shadow snapshot has invalid fields")
    if type(local_binding["version"]) is not int or local_binding["version"] != 1:
        raise ValueError("unsupported local pairing shadow version")
    _pair_id(local_binding["pairing_id"])
    _key_sha(local_binding["owner_key_sha256"])
    _epoch(local_binding["epoch"])
    HostIdentity(local_binding["host_id"], local_binding["installation_id"])
    if type(local_binding["enabled"]) is not bool:
        raise ValueError("local shadow enabled must be boolean")
    for field in ("expires_at", "revocation_checked_at"):
        value = local_binding[field]
        if type(value) is not int or not 0 <= value <= 2**53:
            raise ValueError("local pairing shadow timestamps must be bounded integers")
    if (type(now_seconds) is not int or now_seconds < 0
            or type(max_revocation_age_seconds) is not int
            or not 0 < max_revocation_age_seconds <= 86400
            or not isinstance(durable_task_ids, (set, frozenset, tuple, list))
            or not isinstance(task, dict)):
        raise ValueError("pairing shadow context is not bounded or valid")
    intent = parse_owner_intent(raw_intent)

    def denied(reason: str) -> PairingShadowInspection:
        return PairingShadowInspection(candidate_matches=False, reason=reason)

    if not local_binding["enabled"]:
        return denied("locally_disabled")
    if local_binding["expires_at"] <= now_seconds:
        return denied("locally_expired")
    if (local_binding["revocation_checked_at"] > now_seconds
            or now_seconds - local_binding["revocation_checked_at"]
            > max_revocation_age_seconds):
        return denied("revocation_freshness_unverified")
    if (local_binding["host_id"] != local_identity.host_id
            or local_binding["installation_id"] != local_identity.installation_id
            or intent["target_host_id"] != local_identity.host_id
            or intent["target_installation_id"] != local_identity.installation_id):
        return denied("installation_mismatch")
    if (intent["pairing_id"] != local_binding["pairing_id"]
            or intent["owner_key_sha256"] != local_binding["owner_key_sha256"]
            or intent["epoch"] != local_binding["epoch"]):
        return denied("owner_binding_mismatch")
    if intent["task_id"] in durable_task_ids:
        return denied("already_seen_task_id")
    if (task.get("id") != intent["task_id"]
            or task.get("target_host_id") != local_identity.host_id
            or task.get("target_installation_id") != local_identity.installation_id):
        return denied("task_target_mismatch")
    try:
        digest = task_digest(task)
    except (TypeError, ValueError, OverflowError):
        return denied("task_payload_unverifiable")
    if digest != intent["task_digest"]:
        return denied("task_digest_mismatch")
    return PairingShadowInspection(
        candidate_matches=True, reason="structural_match_only_unsigned",
    )
