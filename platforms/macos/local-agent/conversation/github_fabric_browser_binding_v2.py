"""Pure, default-deny v2 parent/browser identity evidence policy.

No GitHub IO, effect creation, parent takeover, Chrome operation or Send permit.
A caller must independently pin and authenticate every input at GitHub CAS.
"""

from __future__ import annotations

import hashlib
import re
from typing import Any

from local_agent.conversation import github_fabric_dispatch as dispatch_policy
from local_agent.conversation import github_fabric_effect_intents as effects
from local_agent.conversation import github_fabric_parent_ownership as parents

SCHEMA_VERSION = 2
BINDING_KIND = "private_parent_browser_binding_candidate_v2"
BINDING_ROOT = parents.ROOT + "browser_bindings/"
RECEIPT_ROOT = "projects/local-agent/workflows/workflow-001/receipts_v2/"
MAX_EPOCHS = 32
MAX_BINDING_BYTES = 4096
MAX_RECEIPT_BYTES = 8192
_SHA = re.compile(r"[0-9a-f]{40}\Z")
_OWNER = re.compile(r"[0-9a-f]{32}\Z")
_CHILD = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,119}\Z")
_URL = re.compile(r"https://chatgpt\.com/c/[A-Za-z0-9_-]{1,200}\Z")
_BINDING_FIELDS = frozenset({
    "schema_version", "kind", "parent_id", "fence_epoch",
    "parent_owner_id", "dispatch_id", "child_request_id",
    "spawn_transaction_id", "effect_id", "driver_session_id",
    "binding_origin_head_sha", "browser_send_authorized",
})
_RECEIPT_KINDS = {
    "claim": "browser_child_claim_v2",
    "ack": "browser_spawn_ack_v2",
    "result": "browser_terminal_result_v2",
}
_RECEIPT_FIELDS = {
    "claim": frozenset({
        "schema_version", "kind", "dispatch_id", "child_request_id",
        "spawn_transaction_id", "parent_id", "parent_owner_id",
        "fence_epoch", "effect_id", "driver_session_id", "binding_digest",
    }),
    "ack": frozenset({
        "schema_version", "kind", "dispatch_id", "child_request_id",
        "spawn_transaction_id", "binding_digest", "bootstrap_digest",
        "child_conversation_url",
    }),
    "result": frozenset({
        "schema_version", "kind", "dispatch_id", "child_request_id",
        "spawn_transaction_id", "binding_digest", "child_conversation_url",
        "assistant_identity", "assistant_text",
    }),
}


def _canonical(value: dict[str, Any], limit: int) -> bytes:
    try:
        encoded = effects._encoded(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("Private Fabric v2 evidence is not canonical JSON") from exc
    if len(encoded) > limit:
        raise ValueError("Private Fabric v2 evidence exceeds size bound")
    return encoded


def _child(
    parent: dict[str, Any], dispatch: dict[str, Any], child_id: str, *,
    require_active: bool,
) -> dict[str, Any]:
    parents.validate(parent)
    dispatch_policy.validate_github_fabric_dispatch(dispatch)
    if (parent["phase"] not in ({"active"} if require_active else {"active", "completed"})
            or parent["id"] != parents.parent_id(dispatch["parent_conversation_url"])
            or parent["parent_conversation_url"] != dispatch["parent_conversation_url"]
            or parent["dispatch_id"] != dispatch["id"]
            or type(parent["fence_epoch"]) is not int
            or not 1 <= parent["fence_epoch"] <= MAX_EPOCHS):
        raise PermissionError("Private Fabric v2 parent epoch not eligible for binding")
    if not isinstance(child_id, str) or not _CHILD.fullmatch(child_id):
        raise ValueError("Private Fabric v2 child path identity invalid")
    for child in dispatch["children"]:
        if child["request_id"] == child_id:
            return child
    raise PermissionError("Private Fabric v2 child is not in indexed dispatch")


def binding_path(parent_id: str, epoch: int, child_request_id: str) -> str:
    parents.path(parent_id)
    if type(epoch) is not int or not 1 <= epoch <= MAX_EPOCHS:
        raise ValueError("Private Fabric v2 binding epoch path invalid")
    if not isinstance(child_request_id, str) or not _CHILD.fullmatch(child_request_id):
        raise ValueError("Private Fabric v2 child path identity invalid")
    return f"{BINDING_ROOT}{parent_id}/{epoch:04d}/{child_request_id}.json"


def receipt_path(dispatch_id: str, child_request_id: str, kind: str) -> str:
    if (not isinstance(dispatch_id, str)
            or not re.fullmatch(r"fabric-[0-9a-f]{32}", dispatch_id)
            or not isinstance(child_request_id, str)
            or not _CHILD.fullmatch(child_request_id)
            or not isinstance(kind, str) or kind not in _RECEIPT_KINDS):
        raise ValueError("Private Fabric v2 receipt path identity invalid")
    return f"{RECEIPT_ROOT}{dispatch_id}/{kind}/{child_request_id}.json"


def validate_binding(
    record: Any, parent: dict[str, Any], dispatch: dict[str, Any], *,
    expected_origin_head_sha: str,
) -> dict[str, Any]:
    if (not isinstance(record, dict) or set(record) != _BINDING_FIELDS
            or type(record["schema_version"]) is not int
            or record["schema_version"] != SCHEMA_VERSION
            or record["kind"] != BINDING_KIND
            or record["browser_send_authorized"] is not False):
        raise ValueError("Private Fabric v2 binding schema/authority invalid")
    child = _child(parent, dispatch, record["child_request_id"], require_active=False)
    if (not isinstance(expected_origin_head_sha, str)
            or not _SHA.fullmatch(expected_origin_head_sha)
            or record["binding_origin_head_sha"] != expected_origin_head_sha
            or record["parent_id"] != parent["id"]
            or type(record["fence_epoch"]) is not int
            or record["fence_epoch"] != parent["fence_epoch"]
            or record["parent_owner_id"] != parent["owner_id"]
            or record["dispatch_id"] != dispatch["id"]
            or record["spawn_transaction_id"] != child["spawn"]["transaction_id"]
            or record["effect_id"] != effects.effect_id(parent, child)
            or not isinstance(record["driver_session_id"], str)
            or not _OWNER.fullmatch(record["driver_session_id"])
            or record["driver_session_id"] == parent["owner_id"]):
        raise ValueError("Private Fabric v2 binding identity mismatch")
    binding_path(parent["id"], parent["fence_epoch"], child["request_id"])
    _canonical(record, MAX_BINDING_BYTES)
    return record


def propose_binding(
    parent: dict[str, Any], dispatch: dict[str, Any], child_request_id: str, *,
    driver_session_id: str, source_head_sha: str,
) -> dict[str, Any]:
    """Return a candidate immutable record, NEVER a writer or Send token."""
    child = _child(parent, dispatch, child_request_id, require_active=True)
    record = {
        "schema_version": SCHEMA_VERSION, "kind": BINDING_KIND,
        "parent_id": parent["id"], "fence_epoch": parent["fence_epoch"],
        "parent_owner_id": parent["owner_id"], "dispatch_id": dispatch["id"],
        "child_request_id": child["request_id"],
        "spawn_transaction_id": child["spawn"]["transaction_id"],
        "effect_id": effects.effect_id(parent, child),
        "driver_session_id": driver_session_id,
        "binding_origin_head_sha": source_head_sha,
        "browser_send_authorized": False,
    }
    return validate_binding(record, parent, dispatch,
                            expected_origin_head_sha=source_head_sha)


def binding_digest(
    record: dict[str, Any], parent: dict[str, Any], dispatch: dict[str, Any], *,
    expected_origin_head_sha: str,
) -> str:
    validate_binding(record, parent, dispatch,
                     expected_origin_head_sha=expected_origin_head_sha)
    return "sha256:" + hashlib.sha256(_canonical(record, MAX_BINDING_BYTES)).hexdigest()


def validate_receipt(
    kind: str, record: Any, binding: dict[str, Any],
    parent: dict[str, Any], dispatch: dict[str, Any], *,
    expected_origin_head_sha: str,
) -> dict[str, Any]:
    """Validate a supplied receipt; cannot attest remote provenance or Send."""
    digest = binding_digest(binding, parent, dispatch,
                            expected_origin_head_sha=expected_origin_head_sha)
    if (not isinstance(kind, str) or kind not in _RECEIPT_KINDS
            or not isinstance(record, dict)
            or set(record) != _RECEIPT_FIELDS[kind]
            or type(record["schema_version"]) is not int
            or record["schema_version"] != SCHEMA_VERSION
            or record["kind"] != _RECEIPT_KINDS[kind]
            or record["dispatch_id"] != binding["dispatch_id"]
            or record["child_request_id"] != binding["child_request_id"]
            or record["spawn_transaction_id"] != binding["spawn_transaction_id"]
            or record["binding_digest"] != digest):
        raise ValueError("Private Fabric v2 receipt schema/binding mismatch")
    if kind == "claim":
        if any(record[k] != binding[k] for k in (
            "parent_id", "parent_owner_id", "fence_epoch",
            "effect_id", "driver_session_id"
        )) or type(record["fence_epoch"]) is not int:
            raise ValueError("Private Fabric v2 claim owner/driver mismatch")
    else:
        url = record["child_conversation_url"]
        if not isinstance(url, str) or not _URL.fullmatch(url):
            raise ValueError("Private Fabric v2 child URL invalid")
        if kind == "ack":
            child = _child(parent, dispatch, binding["child_request_id"], require_active=False)
            if record["bootstrap_digest"] != child["spawn"]["bootstrap_digest"]:
                raise ValueError("Private Fabric v2 bootstrap digest mismatch")
        elif (not isinstance(record["assistant_identity"], str)
              or not 1 <= len(record["assistant_identity"]) <= 256
              or not isinstance(record["assistant_text"], str)
              or not record["assistant_text"].strip()
              or len(record["assistant_text"]) > 6000):
            raise ValueError("Private Fabric v2 terminal result invalid")
    _canonical(record, MAX_RECEIPT_BYTES)
    return record


def receipt_projection(
    parent: dict[str, Any], dispatch: dict[str, Any],
    binding: dict[str, Any], receipts: dict[str, Any], *,
    expected_origin_head_sha: str,
) -> dict[str, Any]:
    """Project read-only states. Caller MUST pin/verify GitHub provenance."""
    validate_binding(binding, parent, dispatch,
                     expected_origin_head_sha=expected_origin_head_sha)
    if not isinstance(receipts, dict) or set(receipts) - set(_RECEIPT_KINDS):
        raise ValueError("Private Fabric v2 receipt set invalid")
    for kind, record in receipts.items():
        validate_receipt(kind, record, binding, parent, dispatch,
                         expected_origin_head_sha=expected_origin_head_sha)
    claim, ack, result = (receipts.get(k) for k in ("claim", "ack", "result"))
    if result is not None and ack is None:
        state = "result_without_ack"
    elif ack is not None and claim is None:
        state = "ack_without_claim"
    elif result is not None and result["child_conversation_url"] != ack["child_conversation_url"]:
        state = "child_url_mismatch"
    elif result is not None:
        state = "completed_evidence_only"
    elif ack is not None:
        state = "acknowledged"
    elif claim is not None:
        state = "claim_only_unknown"
    else:
        state = "pending"
    return {
        "schema_version": SCHEMA_VERSION,
        "child_request_id": binding["child_request_id"],
        "state": state,
        "requires_reconciliation": state in {
            "result_without_ack", "ack_without_claim", "child_url_mismatch",
            "claim_only_unknown",
        },
        "browser_send_authorized": False,
    }
