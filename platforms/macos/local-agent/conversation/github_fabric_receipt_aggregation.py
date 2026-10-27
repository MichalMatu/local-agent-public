"""Read-only, fail-closed aggregation of immutable private Fabric child evidence.

This is an evidence projection, not a browser worker, result writer, retry
scheduler, or authorization source. Absence of an ACK never proves no Send.
It deliberately never projects bootstrap text or raw assistant responses.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Mapping
from typing import Any

from local_agent.conversation.github_fabric_dispatch import validate_github_fabric_dispatch

_CHILD_URL = re.compile(r"https://chatgpt\.com/c/[A-Za-z0-9_-]{1,200}\Z")
_OWNER = re.compile(r"[0-9a-f]{32}\Z")
_FIELDS = {
    "claim": frozenset({
        "schema_version", "kind", "dispatch_id", "child_request_id",
        "spawn_transaction_id", "parent_conversation_url", "owner_id",
    }),
    "ack": frozenset({
        "schema_version", "kind", "dispatch_id", "child_request_id",
        "spawn_transaction_id", "bootstrap_digest", "child_conversation_url",
    }),
    "result": frozenset({
        "schema_version", "kind", "dispatch_id", "child_request_id",
        "spawn_transaction_id", "child_conversation_url", "assistant_identity",
        "assistant_text",
    }),
}
_KINDS = {
    "claim": "browser_child_claim",
    "ack": "browser_spawn_ack",
    "result": "browser_terminal_result",
}


def _valid_record(
    record: Any, kind: str, dispatch: Mapping[str, Any], child: Mapping[str, Any],
) -> bool:
    if not isinstance(record, dict) or set(record) != _FIELDS[kind]:
        return False
    if (type(record["schema_version"]) is not int
            or record["schema_version"] != 1
            or record["kind"] != _KINDS[kind]
            or record["dispatch_id"] != dispatch["id"]
            or record["child_request_id"] != child["request_id"]
            or record["spawn_transaction_id"] != child["spawn"]["transaction_id"]):
        return False
    if kind == "claim":
        return (
            record["parent_conversation_url"] == dispatch["parent_conversation_url"]
            and isinstance(record["owner_id"], str)
            and bool(_OWNER.fullmatch(record["owner_id"]))
        )
    if (not isinstance(record["child_conversation_url"], str)
            or not _CHILD_URL.fullmatch(record["child_conversation_url"])):
        return False
    if kind == "ack":
        return record["bootstrap_digest"] == child["spawn"]["bootstrap_digest"]
    return (
        isinstance(record["assistant_identity"], str)
        and 0 < len(record["assistant_identity"]) <= 256
        and isinstance(record["assistant_text"], str)
        and bool(record["assistant_text"].strip())
        and len(record["assistant_text"]) <= 6000
    )


def aggregate_private_child_receipts(
    dispatch: dict[str, Any],
    receipts: Mapping[str, Mapping[str, Any]],
    *, expected_owner_id: str | None = None,
) -> dict[str, Any]:
    """Produce bounded, non-sensitive per-child states from one pinned snapshot.

    `receipts` is indexed by immutable child request ID and then by receipt
    kind. `expected_owner_id` must be set by the parent/epoch CAS reader;
    a format-valid but foreign claim must not complete the owner's epoch.
    Callers must read all records at one Git commit; mixing moving heads
    cannot establish a consistent snapshot. Unknown/invalid evidence is never
    repaired, retried or interpreted as proof that no browser Send occurred.
    """
    validate_github_fabric_dispatch(dispatch)
    if (expected_owner_id is not None
            and (not isinstance(expected_owner_id, str)
                 or not _OWNER.fullmatch(expected_owner_id))):
        raise ValueError("Private Fabric expected receipt owner identity invalid")
    if not isinstance(receipts, Mapping):
        raise ValueError("Private Fabric receipts must be a mapping")
    ids = {child["request_id"] for child in dispatch["children"]}
    if set(receipts) - ids:
        raise ValueError("Private Fabric evidence contains unknown child identity")

    children: list[dict[str, str]] = []
    urls: dict[str, list[int]] = {}
    for child in dispatch["children"]:
        child_id = child["request_id"]
        evidence = receipts.get(child_id, {})
        state = "pending"
        reason = ""
        url = ""
        if not isinstance(evidence, Mapping) or set(evidence) - set(_FIELDS):
            state, reason = "invalid_evidence", "invalid_receipt_set"
        else:
            claim = evidence.get("claim")
            ack = evidence.get("ack")
            result = evidence.get("result")
            if any(
                not _valid_record(evidence[kind], kind, dispatch, child)
                for kind in ("claim", "ack", "result") if kind in evidence
            ):
                state, reason = "invalid_evidence", "invalid_receipt"
            elif (claim is not None and expected_owner_id is not None
                  and claim["owner_id"] != expected_owner_id):
                state, reason = "invalid_evidence", "owner_id_conflict"
            elif result is not None and ack is None:
                state, reason = "invalid_evidence", "result_without_ack"
            elif ack is not None and claim is None:
                state, reason = "invalid_evidence", "ack_without_claim"
            elif result is not None and (
                result["child_conversation_url"] != ack["child_conversation_url"]
            ):
                state, reason = "invalid_evidence", "child_url_mismatch"
            elif result is not None:
                state = "completed"
                url = result["child_conversation_url"]
            elif ack is not None:
                state = "acknowledged"
                url = ack["child_conversation_url"]
            elif claim is not None:
                # A claim-only child may have reached Send; do not call it unsent.
                state = "claim_only_unknown"
        entry = {"child_request_id": child_id, "state": state, "reason": reason}
        if url:
            urls.setdefault(url, []).append(len(children))
        children.append(entry)

    # One physical chat must not silently satisfy two distinct transactions.
    for owners in urls.values():
        if len(owners) > 1:
            for index in owners:
                children[index]["state"] = "invalid_evidence"
                children[index]["reason"] = "shared_child_url"

    counts = Counter(item["state"] for item in children)
    if counts["invalid_evidence"]:
        overall = "partial_failure" if counts["completed"] else "invalid_evidence"
    elif counts["completed"] == len(children):
        overall = "completed"
    elif counts["completed"]:
        overall = "partial"
    elif counts["pending"] == len(children):
        overall = "pending"
    else:
        overall = "in_progress"

    return {
        "schema_version": 1,
        "dispatch_id": dispatch["id"],
        "state": overall,
        "children": children,
        "completed": counts["completed"],
        "total": len(children),
        "requires_reconciliation": bool(
            counts["invalid_evidence"] or counts["claim_only_unknown"]
        ),
    }



def aggregate_owner_bound_private_child_receipts(
    dispatch: dict[str, Any],
    receipts: Mapping[str, Mapping[str, Any]],
    *,
    owner_id: str,
) -> dict[str, Any]:
    """Owner-scoped evidence for candidate parent/effect CAS decisions.

    Unlike unbound, informational receipt summaries, this entrypoint
    requires the caller to supply an independently authenticated owner ID
    from the SAME commit-pinned parent epoch. It does not authorize Send.
    """
    if not isinstance(owner_id, str) or not _OWNER.fullmatch(owner_id):
        raise ValueError("Private Fabric owner-bound receipt identity invalid")
    return aggregate_private_child_receipts(
        dispatch, receipts, expected_owner_id=owner_id
    )
