"""Synthetic-only GitHub Fabric terminal-result projection contract.

This does NOT attest that a browser read a message, submitted a prompt, or
observed an actual terminal response. The only permitted source is the
public, hard-coded test fixture. It is not an execution authorization.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any, Iterable, Mapping

from local_agent.conversation import (
    github_fabric_dispatch as dispatch_model,
    github_fabric_publication as publication,
    operator_contract,
)

SCHEMA_VERSION = 1
EVIDENCE_ROOT = ".agent/conversation/browser_results/"
INDEX_PATH = EVIDENCE_ROOT + "index.json"
MAX_RESULTS = 4
MAX_RESULT_BYTES = 8192
MAX_INDEX_BYTES = 4096
SOURCE_KIND = "public_synthetic_unattested_terminal_fixture"
ACK_STATE = "not_attested"
EXECUTION_STATE = "not_attested"
_RESULT_ID = re.compile(r"result-[0-9a-f]{32}\Z")
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
_DISPATCH_ID = re.compile(r"fabric-[0-9a-f]{32}\Z")
_TRANSACTION_ID = re.compile(r"spawn-[0-9a-f]{64}\Z")
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,119}\Z")
_HEAD = re.compile(r"[0-9a-f]{40}\Z")
_FIELDS = frozenset({
    "schema_version", "id", "kind", "dispatch_id", "operator_request_id",
    "operator_request_digest", "workflow_id", "parent_conversation_url",
    "operator_result_digest", "operator_result_state", "ack_state",
    "execution_state", "children",
})
_CHILD_FIELDS = frozenset({
    "request_id", "workflow_node_id", "spawn_transaction_id",
    "operator_child_state", "evidence_digest",
})

# Deliberately fabricated, public, bounded fixture values. These are NOT
# evidence that ChatGPT child conversations have actually completed.
_PUBLIC_FIXTURE_CHILDREN = {
    "child-research": (
        "https://chatgpt.com/c/00000000-0000-4000-8000-000000000001",
        "sha256:" + "a" * 64,
        "PUBLIC SYNTHETIC RESEARCH RESULT",
    ),
    "child-verify": (
        "https://chatgpt.com/c/00000000-0000-4000-8000-000000000002",
        "sha256:" + "b" * 64,
        "PUBLIC SYNTHETIC VERIFICATION RESULT",
    ),
}


def _canonical(value: Any) -> bytes:
    try:
        return json.dumps(
            value, sort_keys=True, separators=(",", ":"),
            ensure_ascii=False, allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ValueError("Fabric synthetic result must be canonical JSON") from exc


def result_id(dispatch_id: str) -> str:
    if not isinstance(dispatch_id, str) or not _DISPATCH_ID.fullmatch(dispatch_id):
        raise ValueError("Fabric result dispatch identity invalid")
    raw = _canonical(["fabric-synthetic-terminal-result-v1", dispatch_id])
    return "result-" + hashlib.sha256(raw).hexdigest()[:32]


def result_path(identifier: str) -> str:
    if not isinstance(identifier, str) or not _RESULT_ID.fullmatch(identifier):
        raise ValueError("Fabric synthetic result path identity invalid")
    return EVIDENCE_ROOT + identifier + ".json"


def validate_index(value: Any) -> dict[str, Any]:
    if value is None:
        return {"schema_version": SCHEMA_VERSION, "result_ids": []}
    if not isinstance(value, dict) or set(value) != {"schema_version", "result_ids"}:
        raise ValueError("Fabric synthetic result index has invalid fields")
    ids = value["result_ids"]
    if (type(value["schema_version"]) is not int or value["schema_version"] != SCHEMA_VERSION
            or not isinstance(ids, list) or len(ids) > MAX_RESULTS
            or any(not isinstance(i, str) or not _RESULT_ID.fullmatch(i) for i in ids)
            or ids != sorted(set(ids)) or len(_canonical(value)) > MAX_INDEX_BYTES):
        raise ValueError("Fabric synthetic result index invalid")
    return {"schema_version": SCHEMA_VERSION, "result_ids": list(ids)}


def validate_projection(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != _FIELDS:
        raise ValueError("Fabric synthetic result projection fields invalid")
    if (type(value["schema_version"]) is not int or value["schema_version"] != SCHEMA_VERSION
            or value["kind"] != SOURCE_KIND
            or value["ack_state"] != ACK_STATE
            or value["execution_state"] != EXECUTION_STATE
            or value["id"] != result_id(value["dispatch_id"])
            or any(not isinstance(value[key], str) or not _ID.fullmatch(value[key])
                   for key in ("operator_request_id", "workflow_id"))
            or any(not isinstance(value[key], str) or not _DIGEST.fullmatch(value[key])
                   for key in ("operator_request_digest", "operator_result_digest"))
            or not isinstance(value["parent_conversation_url"], str)
            or not re.fullmatch(r"https://chatgpt\.com/c/[A-Za-z0-9_-]{1,200}",
                                value["parent_conversation_url"])
            or value["operator_result_state"] != "completed"
            or not isinstance(value["children"], list)
            or not 1 <= len(value["children"]) <= 4
            or len(_canonical(value)) > MAX_RESULT_BYTES):
        raise ValueError("Fabric synthetic result projection invalid")
    seen = set()
    for child in value["children"]:
        if not isinstance(child, dict) or set(child) != _CHILD_FIELDS:
            raise ValueError("Fabric result child projection fields invalid")
        if (any(not isinstance(child[k], str) or not _ID.fullmatch(child[k])
                for k in ("request_id", "workflow_node_id"))
                or not isinstance(child["spawn_transaction_id"], str)
                or not _TRANSACTION_ID.fullmatch(child["spawn_transaction_id"])
                or child["operator_child_state"] != "completed"
                or not isinstance(child["evidence_digest"], str)
                or not _DIGEST.fullmatch(child["evidence_digest"])
                or child["request_id"] in seen):
            raise ValueError("Fabric synthetic result child projection invalid")
        seen.add(child["request_id"])
    return value


def _require_exact_public_fixture(operator_request: dict[str, Any],
                                  child_requests: list[dict[str, Any]]) -> None:
    publication.preflight_synthetic_publication(
        operator_request, child_requests,
        existing_record=None, existing_index=None,
        expected_head_sha="0" * 40, enabled=True, writer_authorized=True,
    )


def public_synthetic_terminal_result(operator_request: dict[str, Any]) -> dict[str, Any]:
    """Build a synthetic test value, never an observed browser result."""
    children = []
    for child in operator_request["children"]:
        ident = child["request_id"]
        if ident not in _PUBLIC_FIXTURE_CHILDREN:
            raise PermissionError("Synthetic result fixture child identity is not approved")
        url, evidence_digest, summary = _PUBLIC_FIXTURE_CHILDREN[ident]
        children.append({
            "request_id": ident,
            "child_state": "retired",
            "summary": summary,
            "child_conversation_url": url,
            "evidence_digest": evidence_digest,
        })
    return operator_contract.build_operator_result(
        operator_request, {"children": children}
    )


def build_projection(
    operator_request: dict[str, Any],
    child_requests: Iterable[dict[str, Any]],
    operator_result: dict[str, Any],
) -> dict[str, Any]:
    requests = list(child_requests)
    _require_exact_public_fixture(operator_request, requests)
    operator_contract.validate_operator_result(operator_result, operator_request)
    expected = public_synthetic_terminal_result(operator_request)
    if _canonical(expected) != _canonical(operator_result):
        raise PermissionError("GitHub Fabric result publication requires exact synthetic fixture")
    dispatch = dispatch_model.build_github_fabric_dispatch(operator_request, requests)
    by_id = {child["request_id"]: child for child in dispatch["children"]}
    result_children = {child["request_id"]: child for child in operator_result["children"]}
    projected = {
        "schema_version": SCHEMA_VERSION,
        "id": result_id(dispatch["id"]),
        "kind": SOURCE_KIND,
        "dispatch_id": dispatch["id"],
        "operator_request_id": dispatch["request_id"],
        "operator_request_digest": dispatch["request_digest"],
        "workflow_id": operator_request["workflow_id"],
        "parent_conversation_url": dispatch["parent_conversation_url"],
        "operator_result_digest": "sha256:" + hashlib.sha256(_canonical(operator_result)).hexdigest(),
        "operator_result_state": operator_result["state"],
        "ack_state": ACK_STATE,
        "execution_state": EXECUTION_STATE,
        "children": [
            {
                "request_id": spec["request_id"],
                "workflow_node_id": spec["node_id"],
                "spawn_transaction_id": by_id[spec["request_id"]]["spawn"]["transaction_id"],
                "operator_child_state": result_children[spec["request_id"]]["state"],
                "evidence_digest": result_children[spec["request_id"]]["evidence_digest"],
            }
            for spec in operator_request["children"]
        ],
    }
    validate_projection(projected)
    return projected


@dataclass(frozen=True, slots=True)
class SyntheticResultPlan:
    operation: str
    expected_head_sha: str
    writes: tuple[tuple[str, dict[str, Any]], ...]


def preflight_synthetic_result(
    operator_request: dict[str, Any],
    child_requests: Iterable[dict[str, Any]],
    operator_result: dict[str, Any],
    *,
    existing_index: dict[str, Any] | None,
    existing_records: Mapping[str, dict[str, Any] | None],
    expected_head_sha: str,
    enabled: bool = False,
    writer_authorized: bool = False,
) -> SyntheticResultPlan:
    if enabled is not True or writer_authorized is not True:
        raise PermissionError("GitHub Fabric synthetic result publication disabled")
    if not isinstance(expected_head_sha, str) or not _HEAD.fullmatch(expected_head_sha):
        raise ValueError("GitHub Fabric result publication requires exact remote head")
    candidate = build_projection(operator_request, child_requests, operator_result)
    index = validate_index(existing_index)
    records = dict(existing_records)
    required = set(index["result_ids"]) | {candidate["id"]}
    if set(records) != required:
        raise ValueError("Synthetic result remote origin snapshot incomplete")
    for identifier, stored in records.items():
        result_path(identifier)
        if stored is not None:
            validate_projection(stored)
            if stored["id"] != identifier:
                raise ValueError("Synthetic result record path mismatch")
        if identifier in index["result_ids"] and stored is None:
            raise ValueError("Synthetic result index references missing record")
        if identifier not in index["result_ids"] and stored is not None:
            raise ValueError("Unindexed synthetic result record requires manual reconciliation")
    prior = records[candidate["id"]]
    if prior is not None:
        if _canonical(prior) != _canonical(candidate):
            raise ValueError("Synthetic result same-ID evidence conflict")
        return SyntheticResultPlan("replay", expected_head_sha, ())
    identifiers = sorted(set(index["result_ids"]) | {candidate["id"]})
    next_index = validate_index({"schema_version": SCHEMA_VERSION, "result_ids": identifiers})
    return SyntheticResultPlan("commit_atomic", expected_head_sha, (
        (result_path(candidate["id"]), candidate), (INDEX_PATH, next_index)
    ))
