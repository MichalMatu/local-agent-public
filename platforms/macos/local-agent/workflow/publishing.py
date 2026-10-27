from __future__ import annotations

import copy
import hashlib
import json
import re
from typing import Any, Iterable

from local_agent.runtime.task_contract import task_digest, validate_task
from local_agent.workflow import contract, revisions

_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")


def child_task_id(
    workflow_id: str,
    node_id: str,
    identity_digest: str,
) -> str:
    """Return a deterministic child id pinned to the node's introduction identity."""
    contract.validate_workflow_id(workflow_id)
    if not isinstance(node_id, str) or not node_id:
        raise ValueError("workflow node id must be a non-empty string")
    if not isinstance(identity_digest, str) or not _DIGEST_RE.fullmatch(identity_digest):
        raise ValueError("identity_digest must be a sha256 digest")
    payload = json.dumps(
        [workflow_id, node_id, identity_digest],
        ensure_ascii=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return "wf-" + hashlib.sha256(payload).hexdigest()


def _task_node(manifest: dict[str, Any], node_id: str) -> dict[str, Any]:
    contract.validate_workflow_manifest(manifest)
    for node in manifest["nodes"]:
        if node["id"] != node_id:
            continue
        if node["kind"] != "task":
            raise ValueError(f"workflow node {node_id!r} is not a task node")
        return node
    raise ValueError(f"unknown workflow node: {node_id!r}")


def materialize_child_task(
    manifest: dict[str, Any],
    node_id: str,
) -> dict[str, Any]:
    """Materialize a child from one immutable static workflow manifest."""
    node = _task_node(manifest, node_id)
    manifest_digest = contract.manifest_digest(manifest)
    task = copy.deepcopy(node["task"])
    task["id"] = child_task_id(str(manifest["id"]), node_id, manifest_digest)
    task["agent_binding"] = node["agent_binding"]
    task["workflow"] = {
        "id": manifest["id"],
        "node_id": node_id,
        "manifest_digest": manifest_digest,
    }
    validate_task(task, require_agent_binding=True)
    return task


def materialized_task_digest(
    manifest: dict[str, Any],
    node_id: str,
) -> str:
    return task_digest(materialize_child_task(manifest, node_id))


def node_introduction_identity(
    base_manifest: dict[str, Any],
    revision_chain: Iterable[dict[str, Any]],
    node_id: str,
) -> tuple[int, str]:
    """Return `(revision, digest)` for the immutable layer that introduced a node.

    Revision 0 means the base manifest. Later effective-graph growth must never change
    this identity, otherwise previously published child ids/digests would become unstable.
    """
    chain = list(revision_chain)
    revisions.validate_revision_sequence(base_manifest, chain)
    for node in base_manifest["nodes"]:
        if node["id"] == node_id:
            return 0, contract.manifest_digest(base_manifest)
    for revision_number, record in enumerate(chain, start=1):
        for node in record["nodes"]:
            if node["id"] == node_id:
                return revision_number, revisions.revision_digest(record)
    raise ValueError(f"unknown workflow node: {node_id!r}")


def _lineage_task_node(
    base_manifest: dict[str, Any],
    revision_chain: list[dict[str, Any]],
    node_id: str,
) -> tuple[dict[str, Any], int, str]:
    revision_number, identity_digest = node_introduction_identity(
        base_manifest,
        revision_chain,
        node_id,
    )
    if revision_number == 0:
        return _task_node(base_manifest, node_id), revision_number, identity_digest
    record = revision_chain[revision_number - 1]
    for node in record["nodes"]:
        if node["id"] != node_id:
            continue
        if node["kind"] != "task":
            raise ValueError(f"workflow node {node_id!r} is not a task node")
        return node, revision_number, identity_digest
    raise AssertionError("validated workflow lineage lost node introduction identity")


def materialize_lineage_child_task(
    base_manifest: dict[str, Any],
    revision_chain: Iterable[dict[str, Any]],
    node_id: str,
) -> dict[str, Any]:
    """Materialize a stable child task from an append-only workflow lineage.

    Base nodes are byte-for-byte equivalent to `materialize_child_task(base, node_id)`.
    Revision nodes are pinned to the digest of the revision that introduced them, not to
    the current effective graph. Appending rev2 therefore cannot change a rev1 child id
    or task digest.
    """
    chain = list(revision_chain)
    node, revision_number, identity_digest = _lineage_task_node(
        base_manifest,
        chain,
        node_id,
    )
    if revision_number == 0:
        return materialize_child_task(base_manifest, node_id)

    task = copy.deepcopy(node["task"])
    task["id"] = child_task_id(str(base_manifest["id"]), node_id, identity_digest)
    task["agent_binding"] = node["agent_binding"]
    task["workflow"] = {
        "id": base_manifest["id"],
        "node_id": node_id,
        "manifest_digest": contract.manifest_digest(base_manifest),
        "revision": revision_number,
        "revision_digest": identity_digest,
    }
    validate_task(task, require_agent_binding=True)
    return task


def materialized_lineage_task_digest(
    base_manifest: dict[str, Any],
    revision_chain: Iterable[dict[str, Any]],
    node_id: str,
) -> str:
    return task_digest(
        materialize_lineage_child_task(base_manifest, revision_chain, node_id)
    )
