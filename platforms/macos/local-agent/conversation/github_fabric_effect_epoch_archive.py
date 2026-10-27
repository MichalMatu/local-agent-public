"""Pure candidate policy for immutable per-epoch effect evidence.

An archive is an OBSERVATION candidate, not a migration, deletion,
parent-epoch takeover, one-shot browser capability or Send authorization.
Only a later, separately reviewed atomic Git Data CAS may publish it.
"""

from __future__ import annotations

import copy
import hashlib
import re
from typing import Any

from local_agent.conversation import github_fabric_dispatch as dispatch_policy
from local_agent.conversation import github_fabric_effect_intents as effects
from local_agent.conversation import github_fabric_parent_ownership as parents

ARCHIVE_KIND = "private_parent_effect_archive_candidate_v1"
ARCHIVE_ROOT = parents.ROOT + "effect_archives/"
MAX_EPOCHS = 32
MAX_ARCHIVE_BYTES = 16384
_SHA = re.compile(r"[0-9a-f]{40}\Z")
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
_FIELDS = {
    "schema_version", "kind", "parent_id", "parent_conversation_url",
    "fence_epoch", "owner_id", "dispatch_id", "source_head_sha",
    "ledger_digest", "ledger", "receipt_summary_digest",
    "receipt_summary", "browser_send_authorized",
}


def archive_path(parent_id: str, epoch: int) -> str:
    parents.path(parent_id)
    if type(epoch) is not int or not 1 <= epoch <= MAX_EPOCHS:
        raise ValueError("Private parent effect archive epoch path invalid")
    return ARCHIVE_ROOT + parent_id + "/" + f"{epoch:04d}.json"


def _require_complete(
    parent: dict[str, Any], dispatch: dict[str, Any], ledger: dict[str, Any],
) -> None:
    parents.validate(parent)
    dispatch_policy.validate_github_fabric_dispatch(dispatch)
    effects.validate_ledger(ledger, parent, dispatch)
    if (parent["phase"] != "completed"
            or parent["completion"] != "verified"
            or len(ledger["effects"]) != len(dispatch["children"])
            or any(row["phase"] != "result_verified" for row in ledger["effects"])):
        raise PermissionError(
            "Private parent effect archive requires completed parent and every verified child"
        )
    archive_path(parent["id"], parent["fence_epoch"])


def _require_pinned_receipts(
    dispatch: dict[str, Any], summary: dict[str, Any], head: str,
) -> None:
    if (not isinstance(summary, dict)
            or set(summary) != {
                "source_head_sha", "schema_version", "dispatch_id",
                "state", "children", "completed", "total",
                "requires_reconciliation",
            }
            or type(summary["schema_version"]) is not int
            or summary["schema_version"] != 1
            or type(summary["completed"]) is not int
            or type(summary["total"]) is not int
            or not isinstance(head, str) or not _SHA.fullmatch(head)
            or summary.get("source_head_sha") != head
            or summary.get("dispatch_id") != dispatch["id"]
            or summary.get("state") != "completed"
            or summary.get("completed") != len(dispatch["children"])
            or summary.get("total") != len(dispatch["children"])
            or summary.get("requires_reconciliation") is not False
            or not isinstance(summary.get("children"), list)
            or len(summary["children"]) != len(dispatch["children"])):
        raise PermissionError("Private parent effect archive pinned receipts incomplete")
    for child, row in zip(dispatch["children"], summary["children"]):
        if (not isinstance(row, dict)
                or set(row) != {"child_request_id", "state", "reason"}
                or row["child_request_id"] != child["request_id"]
                or row["state"] != "completed"
                or row["reason"] != ""):
            raise PermissionError("Private parent effect archive child receipt mismatch")


def validate_archive(
    record: Any, parent: dict[str, Any], dispatch: dict[str, Any],
) -> dict[str, Any]:
    if (not isinstance(record, dict) or set(record) != _FIELDS
            or type(record["schema_version"]) is not int
            or record["schema_version"] != 1 or record["kind"] != ARCHIVE_KIND
            or record["browser_send_authorized"] is not False
            or not isinstance(record["source_head_sha"], str)
            or not _SHA.fullmatch(record["source_head_sha"])
            or not isinstance(record["ledger_digest"], str)
            or not _DIGEST.fullmatch(record["ledger_digest"])
            or not isinstance(record["receipt_summary_digest"], str)
            or not _DIGEST.fullmatch(record["receipt_summary_digest"])):
        raise ValueError("Private parent effect archive header invalid")
    _require_complete(parent, dispatch, record["ledger"])
    _require_pinned_receipts(
        dispatch, record["receipt_summary"], record["source_head_sha"]
    )
    if (record["parent_id"] != parent["id"]
            or record["parent_conversation_url"] != parent["parent_conversation_url"]
            or type(record["fence_epoch"]) is not int
            or record["fence_epoch"] != parent["fence_epoch"]
            or record["owner_id"] != parent["owner_id"]
            or record["dispatch_id"] != parent["dispatch_id"]
            or record["ledger_digest"] !=
            "sha256:" + hashlib.sha256(effects._encoded(record["ledger"])).hexdigest()
            or record["receipt_summary_digest"] !=
            "sha256:" + hashlib.sha256(
                effects._encoded(record["receipt_summary"])
            ).hexdigest()
            or len(effects._encoded(record)) > MAX_ARCHIVE_BYTES):
        raise ValueError("Private parent effect archive evidence mismatch")
    return record


def propose_completed_archive(
    parent: dict[str, Any], dispatch: dict[str, Any],
    ledger: dict[str, Any], *, pinned_summary: dict[str, Any],
    source_head_sha: str,
) -> dict[str, Any]:
    """Propose only; no GitHub IO or permission to remove the active ledger.

    The eventual writer MUST generate pinned_summary from the authenticated
    source commit itself and atomically compare expected Git HEAD. A supplied
    synthetic summary alone is NOT independent receipt evidence.
    """
    _require_complete(parent, dispatch, ledger)
    _require_pinned_receipts(dispatch, pinned_summary, source_head_sha)
    # Freeze the caller's redacted summary by value; never retain mutable
    # references to a source that could change after policy validation.
    summary_copy = copy.deepcopy(pinned_summary)
    ledger_copy = copy.deepcopy(ledger)
    record = {
        "schema_version": 1, "kind": ARCHIVE_KIND,
        "parent_id": parent["id"],
        "parent_conversation_url": parent["parent_conversation_url"],
        "fence_epoch": parent["fence_epoch"], "owner_id": parent["owner_id"],
        "dispatch_id": parent["dispatch_id"],
        "source_head_sha": source_head_sha,
        "ledger_digest": "sha256:" + hashlib.sha256(
            effects._encoded(ledger_copy)
        ).hexdigest(),
        "ledger": ledger_copy,
        "receipt_summary_digest": "sha256:" + hashlib.sha256(
            effects._encoded(summary_copy)
        ).hexdigest(),
        "receipt_summary": summary_copy,
        "browser_send_authorized": False,
    }
    return validate_archive(record, parent, dispatch)


def archive_projection(
    record: dict[str, Any], parent: dict[str, Any],
    dispatch: dict[str, Any],
) -> dict[str, Any]:
    valid = validate_archive(record, parent, dispatch)
    return {
        "parent_id": valid["parent_id"], "epoch": valid["fence_epoch"],
        "status": "immutable_archive_candidate_no_takeover",
        "browser_send_authorized": False,
    }
