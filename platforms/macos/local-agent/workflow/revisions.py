from __future__ import annotations

import copy
import hashlib
import json
import re
from datetime import datetime, timezone
from typing import Any, Iterable

from local_agent.workflow import contract

REVISION_SCHEMA_VERSION = 1
MAX_WORKFLOW_REVISIONS = 32
MAX_REVISION_FILE_BYTES = 2 * 1024 * 1024
MAX_CHECKPOINT_NODE_ID_CHARS = contract.MAX_NODE_ID_CHARS

_REVISION_FIELDS = {
    "schema_version",
    "workflow_id",
    "revision",
    "created_at",
    "parent_digest",
    "checkpoint_node_id",
    "nodes",
}
_NODE_ID_RE = re.compile(r"^[A-Za-z0-9._-]+$")


def _canonical_bytes(payload: Any) -> bytes:
    try:
        text = json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("workflow revision must be canonical JSON data") from exc
    return text.encode("utf-8")


def _validate_created_at(value: Any) -> str:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise ValueError("revision created_at must be an RFC3339 UTC timestamp ending in Z")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise ValueError("revision created_at must be a valid RFC3339 UTC timestamp") from exc
    if parsed.tzinfo != timezone.utc:
        raise ValueError("revision created_at must use UTC")
    return value


def _validate_checkpoint_id(value: Any) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > MAX_CHECKPOINT_NODE_ID_CHARS
        or not _NODE_ID_RE.fullmatch(value)
    ):
        raise ValueError("checkpoint_node_id must be a bounded canonical node id")
    return value


def revision_digest(record: dict[str, Any]) -> str:
    """Return the immutable digest for one revision record.

    Structural/lineage validation is intentionally separate because a revision can only
    be validated relative to its base manifest and prior revisions.
    """
    if not isinstance(record, dict):
        raise ValueError("workflow revision must be an object")
    encoded = _canonical_bytes(record)
    if len(encoded) > MAX_REVISION_FILE_BYTES:
        raise ValueError(f"workflow revision exceeds {MAX_REVISION_FILE_BYTES} bytes")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def lineage_tip_digest(
    base_manifest: dict[str, Any],
    revisions: Iterable[dict[str, Any]],
) -> str:
    sequence = list(revisions)
    validate_revision_sequence(base_manifest, sequence)
    if not sequence:
        return contract.manifest_digest(base_manifest)
    return revision_digest(sequence[-1])


def _effective_manifest_unvalidated(
    base_manifest: dict[str, Any],
    revisions: Iterable[dict[str, Any]],
) -> dict[str, Any]:
    effective = copy.deepcopy(base_manifest)
    nodes = list(effective["nodes"])
    for revision in revisions:
        nodes.extend(copy.deepcopy(revision["nodes"]))
    effective["nodes"] = nodes
    return effective


def _require_new_nodes_descend_from_checkpoint(
    candidate: dict[str, Any],
    *,
    new_node_ids: set[str],
    checkpoint_id: str,
) -> None:
    dependencies = {
        str(node["id"]): tuple(str(item) for item in node["depends_on"])
        for node in candidate["nodes"]
    }
    memo: dict[str, bool] = {checkpoint_id: True}

    def descends(node_id: str) -> bool:
        cached = memo.get(node_id)
        if cached is not None:
            return cached
        result = any(
            dependency == checkpoint_id or descends(dependency)
            for dependency in dependencies[node_id]
        )
        memo[node_id] = result
        return result

    bypassing = sorted(node_id for node_id in new_node_ids if not descends(node_id))
    if bypassing:
        raise ValueError(
            "workflow revision nodes must descend from checkpoint "
            f"{checkpoint_id!r}: {bypassing!r}"
        )


def validate_revision_sequence(
    base_manifest: dict[str, Any],
    revisions: Iterable[dict[str, Any]],
) -> None:
    """Validate one append-only continuation chain against an immutable base manifest."""
    contract.validate_workflow_manifest(base_manifest)
    sequence = list(revisions)
    if len(sequence) > MAX_WORKFLOW_REVISIONS:
        raise ValueError(f"workflow revisions exceeds {MAX_WORKFLOW_REVISIONS} items")

    prior: list[dict[str, Any]] = []
    expected_parent = contract.manifest_digest(base_manifest)
    workflow_id = str(base_manifest["id"])

    for expected_revision, record in enumerate(sequence, start=1):
        if not isinstance(record, dict):
            raise ValueError("workflow revisions must be objects")
        encoded = _canonical_bytes(record)
        if len(encoded) > MAX_REVISION_FILE_BYTES:
            raise ValueError(f"workflow revision exceeds {MAX_REVISION_FILE_BYTES} bytes")
        if set(record) != _REVISION_FIELDS:
            raise ValueError("workflow revision fields do not match schema")
        if (
            type(record.get("schema_version")) is not int
            or record["schema_version"] != REVISION_SCHEMA_VERSION
        ):
            raise ValueError(
                f"workflow revision schema_version must be {REVISION_SCHEMA_VERSION}"
            )
        if record.get("workflow_id") != workflow_id:
            raise ValueError("workflow revision workflow_id does not match base manifest")
        if type(record.get("revision")) is not int or record["revision"] != expected_revision:
            raise ValueError(
                f"workflow revision number must be contiguous; expected {expected_revision}"
            )
        _validate_created_at(record.get("created_at"))
        if record.get("parent_digest") != expected_parent:
            raise ValueError(
                f"workflow revision parent_digest mismatch at revision {expected_revision}"
            )
        checkpoint_id = _validate_checkpoint_id(record.get("checkpoint_node_id"))
        nodes = record.get("nodes")
        if not isinstance(nodes, list) or not nodes:
            raise ValueError("workflow revision nodes must be a non-empty list")

        before = _effective_manifest_unvalidated(base_manifest, prior)
        existing = {str(node["id"]): node for node in before["nodes"]}
        checkpoint = existing.get(checkpoint_id)
        if checkpoint is None:
            raise ValueError(
                f"workflow revision checkpoint {checkpoint_id!r} does not exist in prior graph"
            )
        if checkpoint.get("kind") != "planner_checkpoint":
            raise ValueError(
                f"workflow revision checkpoint {checkpoint_id!r} is not a planner_checkpoint"
            )

        candidate = _effective_manifest_unvalidated(base_manifest, [*prior, record])
        # Reuse the real workflow contract for new node schemas, global id uniqueness,
        # dependency validity, graph acyclicity, task bounds and method structure.
        contract.validate_workflow_manifest(candidate)
        new_node_ids = {str(node["id"]) for node in nodes}
        _require_new_nodes_descend_from_checkpoint(
            candidate,
            new_node_ids=new_node_ids,
            checkpoint_id=checkpoint_id,
        )

        prior.append(record)
        expected_parent = revision_digest(record)


def effective_manifest(
    base_manifest: dict[str, Any],
    revisions: Iterable[dict[str, Any]],
) -> dict[str, Any]:
    """Build the immutable effective graph produced by an append-only revision chain."""
    sequence = list(revisions)
    validate_revision_sequence(base_manifest, sequence)
    return _effective_manifest_unvalidated(base_manifest, sequence)


def validate_effective_method_complete(
    base_manifest: dict[str, Any],
    revisions: Iterable[dict[str, Any]],
) -> dict[str, Any]:
    """Require the effective lineage to satisfy the full pinned Method contract."""
    effective = effective_manifest(base_manifest, revisions)
    contract.validate_complete_workflow_manifest(effective)
    return effective
