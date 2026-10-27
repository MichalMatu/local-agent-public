"""Candidate private per-parent browser-effect intent ledger.

An accepted intent is ALWAYS treated as having possibly reached Send.
It cannot be replayed, deleted, expired, transferred or implicitly retried.
All operations in this module are pure; NO function grants browser authority.
Legacy DOM cannot be admitted until it uses the same parent ownership.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from local_agent.conversation import github_fabric_dispatch as dispatch_contract
from local_agent.conversation import github_fabric_parent_ownership as parent_policy

SCHEMA_VERSION = 1
MAX_EFFECTS = 4
MAX_RECORD_BYTES = 8192
EFFECT_ROOT = parent_policy.ROOT + "effects/"
_HEX = re.compile(r"[0-9a-f]{40}\Z")
_EFFECT = re.compile(r"effect-[0-9a-f]{32}\Z")
_KNOWN_STATES = {"send_unknown", "result_verified", "frozen_unknown"}
_FIELDS = {
    "schema_version", "parent_id", "parent_conversation_url", "fence_epoch",
    "owner_id", "dispatch_id", "revision", "effects",
}
_ENTRY_FIELDS = {
    "effect_id", "child_request_id", "spawn_transaction_id",
    "phase", "result_source_head_sha",
}


def effect_path(parent_id: str) -> str:
    parent_policy.path(parent_id)
    return EFFECT_ROOT + parent_id + ".json"


def _encoded(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def effect_id(parent: dict[str, Any], child: dict[str, Any]) -> str:
    parent_policy.validate(parent)
    values = [
        "fabric-parent-send-intent-v1", parent["id"], parent["fence_epoch"],
        parent["owner_id"], parent["dispatch_id"], child["request_id"],
        child["spawn"]["transaction_id"],
    ]
    return "effect-" + hashlib.sha256(_encoded(values)).hexdigest()[:32]


def validate_ledger(
    record: Any, parent: dict[str, Any], dispatch: dict[str, Any],
) -> dict[str, Any]:
    parent_policy.validate(parent)
    dispatch_contract.validate_github_fabric_dispatch(dispatch)
    if (parent["id"] != parent_policy.parent_id(dispatch["parent_conversation_url"])
            or parent["dispatch_id"] != dispatch["id"]
            or not isinstance(record, dict) or set(record) != _FIELDS
            or type(record["schema_version"]) is not int
            or record["schema_version"] != SCHEMA_VERSION
            or record["parent_id"] != parent["id"]
            or record["parent_conversation_url"] != parent["parent_conversation_url"]
            or type(record["fence_epoch"]) is not int
            or record["fence_epoch"] != parent["fence_epoch"]
            or record["owner_id"] != parent["owner_id"]
            or record["dispatch_id"] != dispatch["id"]
            or type(record["revision"]) is not int or record["revision"] < 1
            or not isinstance(record["effects"], list)
            or not 1 <= len(record["effects"]) <= min(MAX_EFFECTS, len(dispatch["children"]))
            or record["revision"] < len(record["effects"])
            or record["revision"] > 2 * len(record["effects"])
            or len(_encoded(record)) > MAX_RECORD_BYTES):
        raise ValueError("Private parent effect ledger header invalid")
    all_previous_verified = True
    confirmations = 0
    freezes = 0
    for index, entry in enumerate(record["effects"]):
        child = dispatch["children"][index]
        if (not isinstance(entry, dict) or set(entry) != _ENTRY_FIELDS
                or entry["child_request_id"] != child["request_id"]
                or entry["spawn_transaction_id"] != child["spawn"]["transaction_id"]
                or entry["effect_id"] != effect_id(parent, child)
                or not isinstance(entry["phase"], str)
                or entry["phase"] not in _KNOWN_STATES
                or not isinstance(entry["result_source_head_sha"], str)
                or (
                    entry["phase"] == "result_verified"
                    and not _HEX.fullmatch(entry["result_source_head_sha"])
                )
                or (
                    entry["phase"] != "result_verified"
                    and entry["result_source_head_sha"] != ""
                )):
            raise ValueError("Private parent effect identity or phase invalid")
        if index > 0 and not all_previous_verified:
            raise ValueError("Private parent effect was armed before predecessor completed")
        confirmations += entry["phase"] == "result_verified"
        freezes += entry["phase"] == "frozen_unknown"
        all_previous_verified = entry["phase"] == "result_verified"
    # Every new effect adds one revision, every verified result adds one.
    if record["revision"] != len(record["effects"]) + confirmations + freezes:
        raise ValueError("Private parent effect ledger revision mismatch")
    return record


def propose_send_intent(
    parent: dict[str, Any], dispatch: dict[str, Any],
    child_request_id: str, *, existing: dict[str, Any] | None = None,
) -> dict[str, Any]:
    parent_policy.validate(parent)
    dispatch_contract.validate_github_fabric_dispatch(dispatch)
    if parent["phase"] != "active":
        raise PermissionError("Private parent effect owner not active")
    # Independently validate the immutable ledger and its parent/dispatch scope.
    if existing is not None:
        validate_ledger(existing, parent, dispatch)
    if not isinstance(child_request_id, str):
        raise ValueError("Private parent effect child identity invalid")
    prior = [] if existing is None else existing["effects"]
    index = len(prior)
    if (index >= len(dispatch["children"])
            or dispatch["children"][index]["request_id"] != child_request_id):
        raise PermissionError("Private parent effect duplicate, skipped or out of order")
    if prior and prior[-1]["phase"] != "result_verified":
        raise PermissionError("Private parent effect predecessor Send unknown")
    child = dispatch["children"][index]
    record = {
        "schema_version": 1, "parent_id": parent["id"],
        "parent_conversation_url": parent["parent_conversation_url"],
        "fence_epoch": parent["fence_epoch"], "owner_id": parent["owner_id"],
        "dispatch_id": parent["dispatch_id"],
        "revision": 1 if existing is None else existing["revision"] + 1,
        "effects": [
            *prior,
            {
                "effect_id": effect_id(parent, child),
                "child_request_id": child["request_id"],
                "spawn_transaction_id": child["spawn"]["transaction_id"],
                "phase": "send_unknown", "result_source_head_sha": "",
            },
        ],
    }
    return validate_ledger(record, parent, dispatch)


def propose_result_verification(
    parent: dict[str, Any], dispatch: dict[str, Any],
    child_request_id: str, pinned_summary: dict[str, Any], *,
    existing: dict[str, Any],
) -> dict[str, Any]:
    validate_ledger(existing, parent, dispatch)
    if parent["phase"] != "active":
        raise PermissionError("Private parent effect owner no longer active")
    latest = existing["effects"][-1]
    if latest["child_request_id"] != child_request_id or latest["phase"] != "send_unknown":
        raise PermissionError("Private parent effect verification replay or wrong child")
    if (not isinstance(pinned_summary, dict)
            or pinned_summary.get("dispatch_id") != dispatch["id"]
            or not isinstance(pinned_summary.get("source_head_sha"), str)
            or not _HEX.fullmatch(pinned_summary["source_head_sha"])
            or not isinstance(pinned_summary.get("children"), list)
            or len(pinned_summary["children"]) != len(dispatch["children"])):
        raise PermissionError("Private parent effect pinned receipt summary invalid")
    for child, row in zip(dispatch["children"], pinned_summary["children"]):
        if not isinstance(row, dict) or row.get("child_request_id") != child["request_id"]:
            raise PermissionError("Private parent effect child receipt identity invalid")
    matching = pinned_summary["children"][len(existing["effects"]) - 1]
    if (set(matching) != {"child_request_id", "state", "reason"}
            or matching["state"] != "completed" or matching["reason"] != ""):
        raise PermissionError("Private parent effect independent terminal result not verified")
    next_record = {
        **existing, "revision": existing["revision"] + 1,
        "effects": [
            *existing["effects"][:-1],
            {
                **latest, "phase": "result_verified",
                "result_source_head_sha": pinned_summary["source_head_sha"],
            },
        ],
    }
    return validate_ledger(next_record, parent, dispatch)


def freeze_unknown(
    parent: dict[str, Any], dispatch: dict[str, Any],
    existing: dict[str, Any],
) -> dict[str, Any]:
    validate_ledger(existing, parent, dispatch)
    last = existing["effects"][-1]
    if last["phase"] == "frozen_unknown":
        return existing
    if last["phase"] != "send_unknown":
        raise PermissionError("Verified parent effect cannot be frozen")
    # A frozen effect is NOT a release. Its intent persists indefinitely and
    # the parent epoch must also be frozen before any takeover protocol.
    result = {
        **existing, "revision": existing["revision"] + 1,
        "effects": [*existing["effects"][:-1], {
            **last, "phase": "frozen_unknown"
        }],
    }
    return validate_ledger(result, parent, dispatch)


def recovery_projection(
    parent: dict[str, Any], dispatch: dict[str, Any],
    existing: dict[str, Any] | None,
) -> dict[str, Any]:
    if existing is None:
        return {
            "parent_id": parent_policy.validate(parent)["id"],
            "status": "unarmed", "browser_send_authorized": False
        }
    validate_ledger(existing, parent, dispatch)
    phase = existing["effects"][-1]["phase"]
    return {
        "parent_id": existing["parent_id"], "revision": existing["revision"],
        "status": {
            "send_unknown": "reconcile_only_no_send_replay",
            "result_verified": "verified_no_send_replay",
            "frozen_unknown": "blocked_unknown_no_takeover",
        }[phase],
        "browser_send_authorized": False,
    }
