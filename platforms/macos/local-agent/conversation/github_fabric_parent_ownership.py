"""Candidate-only private Fabric global-parent fencing policy (NO Send authority).

This is a pure preview for future atomic private-GitHub CAS publication and
universal legacy-driver admission. Neither creating a proposal nor reading it
allows opening tabs, submitting prompts, or replacing an active controller.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from local_agent.conversation.github_fabric_dispatch import (
    validate_github_fabric_dispatch,
)

KIND = "private_parent_ownership_candidate_v1"
ROOT = "projects/local-agent/parent_ownership/"
_HEX = re.compile(r"[0-9a-f]{32}\Z")
_SHA = re.compile(r"[0-9a-f]{40}\Z")
_DISPATCH = re.compile(r"fabric-[0-9a-f]{32}\Z")
_ID = re.compile(r"parent-[0-9a-f]{32}\Z")
_FIELDS = frozenset({
    "schema_version", "kind", "id", "parent_conversation_url",
    "fence_epoch", "owner_id", "dispatch_id", "phase", "completion",
})
_STATES = frozenset({"active", "completed", "unknown_frozen"})


def parent_id(parent_url: str) -> str:
    # Keep exactly the same deterministic parent identity as the existing
    # default-disabled JavaScript synthetic parent-fence reader.
    if not isinstance(parent_url, str) or not re.fullmatch(
        r"https://chatgpt\.com/c/[A-Za-z0-9_-]{1,200}", parent_url
    ):
        raise ValueError("Private Fabric parent URL invalid")
    material = json.dumps(
        ["fabric-global-parent-mode-v1", parent_url],
        separators=(",", ":"), ensure_ascii=False,
    ).encode("utf-8")
    return "parent-" + hashlib.sha256(material).hexdigest()[:32]


def path(identifier: str) -> str:
    if not isinstance(identifier, str) or not _ID.fullmatch(identifier):
        raise ValueError("Private Fabric parent fence identity invalid")
    return ROOT + identifier + ".json"


def validate(record: Any) -> dict[str, Any]:
    if not isinstance(record, dict) or set(record) != _FIELDS:
        raise ValueError("Private Fabric parent fence fields invalid")
    url = record.get("parent_conversation_url")
    if (type(record["schema_version"]) is not int
            or record["schema_version"] != 1
            or record["kind"] != KIND
            or record["id"] != parent_id(url)
            or type(record["fence_epoch"]) is not int
            or not 1 <= record["fence_epoch"] <= 2**31 - 1
            or not isinstance(record["owner_id"], str)
            or not _HEX.fullmatch(record["owner_id"])
            or not isinstance(record["dispatch_id"], str)
            or not _DISPATCH.fullmatch(record["dispatch_id"])
            or not isinstance(record["phase"], str)
            or record["phase"] not in _STATES
            or record["completion"] != {
                "active": "none",
                "completed": "verified",
                "unknown_frozen": "unresolved",
            }[record["phase"]]
            or len(json.dumps(record, sort_keys=True).encode("utf-8")) > 4096):
        raise ValueError("Private Fabric parent fence invalid or unresolved")
    return record


def propose_acquisition(
    dispatch: dict[str, Any],
    owner_id: str,
    *,
    existing: dict[str, Any] | None = None,
) -> tuple[str, dict[str, Any]]:
    """Return a deterministic CAS *proposal*, not a browser authorization.

    A different controller/dispatch cannot replace an active or uncertain
    predecessor. No wall-clock TTL, tab inspection, or lost ACK is allowed
    to authorize takeover. The future Git writer must atomically replace
    the exact parent record and index on a non-force fast-forward ref.
    """
    validate_github_fabric_dispatch(dispatch)
    if not isinstance(owner_id, str) or not _HEX.fullmatch(owner_id):
        raise ValueError("Private Fabric parent owner invalid")
    identifier = parent_id(dispatch["parent_conversation_url"])
    prior = None if existing is None else validate(existing)
    if prior is not None:
        if prior["id"] != identifier:
            raise PermissionError("Private Fabric parent identity conflict")
        if prior["phase"] == "active":
            if (prior["owner_id"] == owner_id
                    and prior["dispatch_id"] == dispatch["id"]):
                return "replay", prior
            raise PermissionError("Private Fabric parent already owned")
        if prior["phase"] == "unknown_frozen":
            raise PermissionError("Private Fabric parent outcome requires manual reconciliation")
        if prior["dispatch_id"] == dispatch["id"]:
            raise PermissionError("Private Fabric completed dispatch may not be rearmed")
        if prior["owner_id"] == owner_id:
            raise PermissionError("Private Fabric new epoch requires a distinct owner identity")
        if prior["fence_epoch"] >= 2**31 - 1:
            raise ValueError("Private Fabric parent fence epoch exhausted")
    epoch = 1 if prior is None else prior["fence_epoch"] + 1
    result = {
        "schema_version": 1, "kind": KIND, "id": identifier,
        "parent_conversation_url": dispatch["parent_conversation_url"],
        "fence_epoch": epoch,
        "owner_id": owner_id, "dispatch_id": dispatch["id"],
        "phase": "active", "completion": "none",
    }
    return "propose_cas", validate(result)


def freeze_uncertain(record: dict[str, Any]) -> dict[str, Any]:
    existing = validate(record)
    if existing["phase"] == "unknown_frozen":
        return dict(existing)
    if existing["phase"] != "active":
        raise ValueError("Private Fabric completed parent cannot become unknown")
    return validate({
        **existing, "phase": "unknown_frozen", "completion": "unresolved"
    })


def propose_completion(
    record: dict[str, Any],
    dispatch: dict[str, Any],
    pinned_summary: dict[str, Any],
) -> dict[str, Any]:
    """Require complete, redacted, single-commit receipt evidence for closure.

    This pure check is insufficient to claim the old browser driver quiesced;
    actual CAS writer and every browser Send path remain unimplemented.
    """
    existing = validate(record)
    validate_github_fabric_dispatch(dispatch)
    if (existing["phase"] != "active"
            or existing["dispatch_id"] != dispatch["id"]
            or existing["parent_conversation_url"] != dispatch["parent_conversation_url"]):
        raise PermissionError("Private Fabric parent completion identity mismatch")
    ids = [child["request_id"] for child in dispatch["children"]]
    if (not isinstance(pinned_summary, dict)
            or not isinstance(pinned_summary.get("source_head_sha"), str)
            or not _SHA.fullmatch(pinned_summary["source_head_sha"])
            or pinned_summary.get("dispatch_id") != dispatch["id"]
            or pinned_summary.get("state") != "completed"
            or pinned_summary.get("completed") != len(ids)
            or pinned_summary.get("total") != len(ids)
            or pinned_summary.get("requires_reconciliation") is not False
            or not isinstance(pinned_summary.get("children"), list)
            or len(pinned_summary["children"]) != len(ids)
            or any(not isinstance(item, dict)
                   or set(item) != {"child_request_id", "state", "reason"}
                   or item["child_request_id"] != child_id
                   or item["state"] != "completed"
                   or item["reason"] != ""
                   for child_id, item in zip(ids, pinned_summary["children"]))):
        raise PermissionError("Private Fabric parent completion requires pinned all-child receipts")
    return validate({**existing, "phase": "completed", "completion": "verified"})
