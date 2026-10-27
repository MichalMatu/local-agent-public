"""Atomic, default-disabled semantic-node claim preflight for public synthetic fixtures.

A claim in this namespace is deliberately *observation-only*, never a browser
execution permit. The existing DOM driver has not joined this authority and
real/private request publication is forbidden. A future production authority
must establish parent-mode exclusion, a private transport and fenced ownership
before issuing any Send permission.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any, Iterable, Mapping

from local_agent.conversation import github_fabric_dispatch, github_fabric_publication

SCHEMA_VERSION = 1
CLAIM_ROOT = ".agent/conversation/browser_claims/"
INDEX_PATH = CLAIM_ROOT + "index.json"
MAX_CLAIMS = 4
MAX_INDEX_BYTES = 4096
MAX_CLAIM_BYTES = 4096
CLAIM_MODE = "synthetic_observation_only"
CLAIM_PHASE = "admission_preview"
_CLAIM_RE = re.compile(r"claim-[0-9a-f]{32}\Z")
_DIGEST_RE = re.compile(r"sha256:[0-9a-f]{64}\Z")
_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,119}\Z")
_SHA_RE = re.compile(r"[0-9a-f]{40}\Z")
_CLAIM_KEYS = frozenset({
    "schema_version", "id", "workflow_id", "workflow_node_id",
    "parent_conversation_url", "operator_request_id",
    "operator_request_digest", "dispatch_id", "child_request_id",
    "child_request_digest", "spawn_transaction_id", "bootstrap_digest",
    "mode", "phase",
})


def _encoded(value: Any) -> bytes:
    try:
        return json.dumps(
            value, sort_keys=True, separators=(",", ":"),
            ensure_ascii=False, allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ValueError("Fabric claim requires canonical JSON") from exc


def claim_id(workflow_id: str, workflow_node_id: str) -> str:
    if not all(isinstance(value, str) and _ID_RE.fullmatch(value)
               for value in (workflow_id, workflow_node_id)):
        raise ValueError("Fabric semantic node identity is invalid")
    material = ["fabric-semantic-node-v1", workflow_id, workflow_node_id]
    return "claim-" + hashlib.sha256(_encoded(material)).hexdigest()[:32]


def claim_path(identifier: str) -> str:
    if not isinstance(identifier, str) or not _CLAIM_RE.fullmatch(identifier):
        raise ValueError("Fabric claim ID is invalid")
    return CLAIM_ROOT + identifier + ".json"


def validate_claim(record: Any) -> dict[str, Any]:
    if not isinstance(record, dict) or set(record) != _CLAIM_KEYS:
        raise ValueError("Fabric semantic claim fields are invalid")
    if type(record["schema_version"]) is not int or record["schema_version"] != SCHEMA_VERSION:
        raise ValueError("Fabric semantic claim schema is invalid")
    if record["id"] != claim_id(record["workflow_id"], record["workflow_node_id"]):
        raise ValueError("Fabric semantic claim identity mismatch")
    for key in ("operator_request_id", "child_request_id"):
        if not isinstance(record[key], str) or not _ID_RE.fullmatch(record[key]):
            raise ValueError(f"Fabric semantic claim {key} invalid")
    for key in ("operator_request_digest", "child_request_digest", "bootstrap_digest"):
        if not isinstance(record[key], str) or not _DIGEST_RE.fullmatch(record[key]):
            raise ValueError(f"Fabric semantic claim {key} invalid")
    if (
        not isinstance(record["parent_conversation_url"], str)
        or not record["parent_conversation_url"].startswith("https://chatgpt.com/c/")
        or not re.fullmatch(r"https://chatgpt.com/c/[A-Za-z0-9_-]{1,200}",
                            record["parent_conversation_url"])
        or not isinstance(record["dispatch_id"], str)
        or not re.fullmatch(r"fabric-[0-9a-f]{32}", record["dispatch_id"])
        or not isinstance(record["spawn_transaction_id"], str)
        or not re.fullmatch(r"spawn-[0-9a-f]{64}", record["spawn_transaction_id"])
        or record["mode"] != CLAIM_MODE
        or record["phase"] != CLAIM_PHASE
        or len(_encoded(record)) > MAX_CLAIM_BYTES
    ):
        raise ValueError("Fabric semantic claim content is invalid")
    return record


def validate_index(value: Any) -> dict[str, Any]:
    if value is None:
        return {"schema_version": SCHEMA_VERSION, "claim_ids": []}
    if not isinstance(value, dict) or set(value) != {"schema_version", "claim_ids"}:
        raise ValueError("Fabric claim index fields are invalid")
    ids = value["claim_ids"]
    if (
        type(value["schema_version"]) is not int
        or value["schema_version"] != SCHEMA_VERSION
        or not isinstance(ids, list)
        or len(ids) > MAX_CLAIMS
        or any(not isinstance(i, str) or not _CLAIM_RE.fullmatch(i) for i in ids)
        or ids != sorted(set(ids))
        or len(_encoded(value)) > MAX_INDEX_BYTES
    ):
        raise ValueError("Fabric claim index is malformed or exceeds capacity")
    return {"schema_version": SCHEMA_VERSION, "claim_ids": list(ids)}


def build_claims(operator_request: dict[str, Any],
                 child_requests: Iterable[dict[str, Any]]) -> tuple[dict[str, Any], ...]:
    """Derive semantic IDs independently of DOM/GitHub transaction algorithms."""
    requests = list(child_requests)
    dispatch = github_fabric_dispatch.build_github_fabric_dispatch(operator_request, requests)
    by_id = {request["id"]: request for request in requests}
    by_spawn_id = {child["request_id"]: child["spawn"] for child in dispatch["children"]}
    records = []
    for child in operator_request["children"]:
        request = by_id[child["request_id"]]
        spawn = by_spawn_id[request["id"]]
        record = {
            "schema_version": SCHEMA_VERSION,
            "id": claim_id(request["workflow_id"], request["workflow_node_id"]),
            "workflow_id": request["workflow_id"],
            "workflow_node_id": request["workflow_node_id"],
            "parent_conversation_url": dispatch["parent_conversation_url"],
            "operator_request_id": dispatch["request_id"],
            "operator_request_digest": dispatch["request_digest"],
            "dispatch_id": dispatch["id"],
            "child_request_id": request["id"],
            "child_request_digest": spawn["child_request_digest"],
            "spawn_transaction_id": spawn["transaction_id"],
            "bootstrap_digest": spawn["bootstrap_digest"],
            "mode": CLAIM_MODE,
            "phase": CLAIM_PHASE,
        }
        validate_claim(record)
        records.append(record)
    return tuple(sorted(records, key=lambda record: record["id"]))


@dataclass(frozen=True, slots=True)
class SyntheticClaimPlan:
    operation: str
    expected_head_sha: str
    writes: tuple[tuple[str, dict[str, Any]], ...]


def preflight_synthetic_claims(
    operator_request: dict[str, Any],
    child_requests: Iterable[dict[str, Any]],
    *,
    existing_index: dict[str, Any] | None,
    existing_records: Mapping[str, dict[str, Any] | None],
    expected_head_sha: str,
    enabled: bool = False,
    writer_authorized: bool = False,
) -> SyntheticClaimPlan:
    if enabled is not True or writer_authorized is not True:
        raise PermissionError("Fabric semantic claims are default-disabled")
    if not isinstance(expected_head_sha, str) or not _SHA_RE.fullmatch(expected_head_sha):
        raise ValueError("Fabric claim publication requires a pinned branch head")
    requests = list(child_requests)
    # The existing public-fixture allowlist is the ONLY publication gate.
    # Never infer that caller-defined synthetic=True makes private text safe.
    github_fabric_publication.preflight_synthetic_publication(
        operator_request, requests, existing_record=None, existing_index=None,
        expected_head_sha=expected_head_sha, enabled=True, writer_authorized=True,
    )
    index = validate_index(existing_index)
    candidates = build_claims(operator_request, requests)
    current_ids = set(index["claim_ids"])
    candidates_by_id = {item["id"]: item for item in candidates}
    records = dict(existing_records)
    required_ids = current_ids | set(candidates_by_id)
    if set(records) != required_ids:
        raise ValueError("Fabric claim origin snapshot is incomplete")
    for identifier, stored in records.items():
        claim_path(identifier)
        if stored is not None:
            validate_claim(stored)
            if stored["id"] != identifier:
                raise ValueError("Fabric claim path identity conflicts")
        if identifier in current_ids and stored is None:
            raise ValueError("Fabric index refers to a missing claim")
        if identifier not in current_ids and stored is not None:
            raise ValueError("Unindexed Fabric claim requires manual reconciliation")
    missing = []
    for identifier, candidate in candidates_by_id.items():
        prior = records[identifier]
        if prior is None:
            missing.append(identifier)
        elif _encoded(prior) != _encoded(candidate):
            raise ValueError("Fabric same-semantic-node claim conflict")
    if not missing:
        return SyntheticClaimPlan("replay", expected_head_sha, ())
    union = sorted(current_ids | set(candidates_by_id))
    next_index = validate_index({"schema_version": SCHEMA_VERSION, "claim_ids": union})
    writes = tuple(
        [(claim_path(i), candidates_by_id[i]) for i in sorted(missing)]
        + [(INDEX_PATH, next_index)]
    )
    return SyntheticClaimPlan("commit_atomic", expected_head_sha, writes)
