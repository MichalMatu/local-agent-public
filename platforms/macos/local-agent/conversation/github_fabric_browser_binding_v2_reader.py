"""Independently commit-pinned read-only v2 browser binding observer.

Each published binding must first appear in the single-parent commit whose
parent is its declared pre-CAS origin. Immutable content must persist along
the bounded ancestry to the pinned head. This is observation only, with
no browser, token persistence, GitHub write or Send authority.
"""

from __future__ import annotations

from typing import Any

from local_agent.conversation import github_fabric_browser_binding_v2 as binding
from local_agent.conversation import github_fabric_github as git
from local_agent.conversation import github_fabric_parent_ownership as policy
from local_agent.conversation import github_fabric_parent_ownership_github as owners

MAX_ANCESTRY_COMMITS = 64


def _proven_lineage(
    api: Any, current_head: str, origin_head: str, path: str,
    expected: dict[str, Any],
) -> list[str]:
    """Authenticate direct origin, unchanged binding and return newest-first ancestry."""
    current = git._require_sha(current_head, label="browser pinned head")
    origin = git._require_sha(origin_head, label="browser binding origin")
    visited: set[str] = set()
    intervening: list[str] = []
    for _ in range(MAX_ANCESTRY_COMMITS):
        if current == origin:
            break
        if current in visited:
            raise ValueError("Private browser binding commit ancestry cycle")
        visited.add(current)
        commit = api.request("GET", f"/git/commits/{current}")
        if not isinstance(commit, dict):
            raise ValueError("Private browser binding commit malformed")
        parents = commit.get("parents")
        if not isinstance(parents, list) or len(parents) != 1:
            raise ValueError("Private browser binding ancestry non-linear")
        previous = git._require_sha(
            parents[0].get("sha") if isinstance(parents[0], dict) else None,
            label="browser binding commit parent",
        )
        intervening.append(current)
        current = previous
    if current != origin or not intervening:
        raise ValueError("Private browser binding origin is not within pinned ancestry")
    if git._read_json_at_commit(
        api, path, origin, max_bytes=binding.MAX_BINDING_BYTES,
    ) is not None:
        raise ValueError("Private browser binding existed before its declared origin")
    for sha in intervening:
        observed = git._read_json_at_commit(
            api, path, sha, max_bytes=binding.MAX_BINDING_BYTES,
        )
        if observed != expected:
            raise ValueError("Private browser binding changed in pinned history")
    return intervening


def _verify_receipt_history(
    api: Any, origin: str, lineage: list[str], dispatch_id: str,
    child_id: str, current_receipts: dict[str, Any],
) -> None:
    """V2 receipts are born after their binding CAS and then immutable.

    Check even absent current receipts, so deletion is not mistaken for pending.
    The caller supplies a verified binding ancestry, newest commit first.
    """
    if not lineage:
        raise ValueError("Private browser receipt binding ancestry absent")
    insertion_commit = lineage[-1]
    for kind in ("claim", "ack", "result"):
        path = binding.receipt_path(dispatch_id, child_id, kind)
        if (git._read_json_at_commit(
            api, path, origin, max_bytes=binding.MAX_RECEIPT_BYTES,
        ) is not None or git._read_json_at_commit(
            api, path, insertion_commit, max_bytes=binding.MAX_RECEIPT_BYTES,
        ) is not None):
            raise ValueError("Private browser receipt predates binding publication")
        seen = False
        for sha in reversed(lineage[:-1]):
            historical = git._read_json_at_commit(
                api, path, sha, max_bytes=binding.MAX_RECEIPT_BYTES,
            )
            if historical is not None:
                if historical != current_receipts.get(kind):
                    raise ValueError("Private browser receipt changed in pinned history")
                seen = True
            elif seen:
                raise ValueError("Private browser receipt disappeared in pinned history")
        if seen != (kind in current_receipts):
            raise ValueError("Private browser receipt history/current head mismatch")


def _verify_origin(
    api: Any, origin_head: str, dispatch_id: str,
    record: dict[str, Any], current_dispatch: dict[str, Any],
) -> None:
    """Verify the indexed dispatch, active parent, history and epoch witness at origin."""
    dispatch = owners._indexed_dispatch(api, origin_head, dispatch_id)
    if dispatch != current_dispatch:
        raise ValueError("Private browser binding origin dispatch changed")
    identifier = policy.parent_id(dispatch["parent_conversation_url"])
    origin_parent = git._read_json_at_commit(
        api, policy.path(identifier), origin_head, max_bytes=owners.MAX_RECORD_BYTES,
    )
    origin_history = git._read_json_at_commit(
        api, owners.history_path(identifier), origin_head,
        max_bytes=owners.MAX_HISTORY_BYTES,
    )
    origin_witness = git._read_json_at_commit(
        api, owners.epoch_path(identifier, record["fence_epoch"]), origin_head,
        max_bytes=owners.MAX_RECORD_BYTES,
    )
    if origin_parent is None or origin_history is None or origin_witness is None:
        raise ValueError("Private browser binding origin owner evidence absent")
    policy.validate(origin_parent)
    owners.validate_history(origin_history, identifier)
    if (origin_parent["phase"] != "active"
            or origin_parent["id"] != record["parent_id"]
            or origin_parent["owner_id"] != record["parent_owner_id"]
            or origin_parent["fence_epoch"] != record["fence_epoch"]
            or origin_parent["dispatch_id"] != record["dispatch_id"]
            or origin_witness != origin_parent
            or origin_history["entries"][-1] != {
                "epoch": record["fence_epoch"],
                "dispatch_id": record["dispatch_id"],
                "owner_id": record["parent_owner_id"],
            }):
        raise ValueError("Private browser binding origin epoch authentication failed")


def _receipts(api: Any, snapshot: owners.Snapshot, child_id: str) -> dict[str, Any]:
    evidence: dict[str, Any] = {}
    for kind in ("claim", "ack", "result"):
        record = git._read_json_at_commit(
            api, binding.receipt_path(snapshot.dispatch["id"], child_id, kind),
            snapshot.head, max_bytes=binding.MAX_RECEIPT_BYTES,
        )
        if record is not None:
            evidence[kind] = record
    return evidence


def _reject_sibling_reuse(
    api: Any, snapshot: owners.Snapshot, selected: dict[str, Any],
    receipts: dict[str, Any],
) -> None:
    parent = snapshot.parent
    assert parent is not None
    # Compare all children, not only every sibling against the selected
    # child: collisions between two non-selected siblings are also unsafe.
    seen_drivers = {selected["driver_session_id"]}
    seen_urls = {
        item["child_conversation_url"] for kind, item in receipts.items()
        if kind in {"ack", "result"} and isinstance(item.get("child_conversation_url"), str)
    }
    for child in snapshot.dispatch["children"]:
        if child["request_id"] == selected["child_request_id"]:
            continue
        path = binding.binding_path(
            parent["id"], parent["fence_epoch"], child["request_id"],
        )
        other = git._read_json_at_commit(
            api, path, snapshot.head, max_bytes=binding.MAX_BINDING_BYTES,
        )
        other_receipts = _receipts(api, snapshot, child["request_id"])
        if other is None:
            if other_receipts:
                raise ValueError("Private browser sibling orphan v2 receipt")
            continue
        driver_id = other.get("driver_session_id")
        if driver_id in seen_drivers:
            raise ValueError("Private browser driver session reused across children")
        binding.validate_binding(
            other, parent, snapshot.dispatch,
            expected_origin_head_sha=other.get("binding_origin_head_sha"),
        )
        sibling_lineage = _proven_lineage(
            api, snapshot.head, other["binding_origin_head_sha"], path, other,
        )
        _verify_receipt_history(
            api, other["binding_origin_head_sha"], sibling_lineage,
            snapshot.dispatch["id"], child["request_id"], other_receipts,
        )
        _verify_origin(
            api, other["binding_origin_head_sha"],
            snapshot.dispatch["id"], other, snapshot.dispatch,
        )
        seen_drivers.add(driver_id)
        sibling_urls: set[str] = set()
        for kind, candidate in other_receipts.items():
            binding.validate_receipt(
                kind, candidate, other, parent, snapshot.dispatch,
                expected_origin_head_sha=other["binding_origin_head_sha"],
            )
            if kind in {"ack", "result"}:
                url = candidate["child_conversation_url"]
                if url in seen_urls:
                    raise ValueError("Private browser child URL reused across siblings")
                sibling_urls.add(url)
        seen_urls.update(sibling_urls)


def read_synthetic_pinned_browser_binding(
    dispatch_id: str, child_request_id: str, *,
    enabled: bool = False, api: Any | None = None,
) -> dict[str, Any]:
    """Redacted pinned projection; synthetic API is explicit and read-only."""
    if enabled is not True or api is None:
        raise PermissionError("Private browser binding pinned reader default-disabled")
    snapshot = owners._snapshot(api, dispatch_id)
    parent = snapshot.parent
    if parent is None or parent["phase"] not in {"active", "completed"}:
        raise PermissionError("Private browser binding parent not eligible for read")
    if child_request_id not in {
        child["request_id"] for child in snapshot.dispatch["children"]
    }:
        raise PermissionError("Private browser binding child not indexed in dispatch")
    path = binding.binding_path(
        parent["id"], parent["fence_epoch"], child_request_id,
    )
    record = git._read_json_at_commit(
        api, path, snapshot.head, max_bytes=binding.MAX_BINDING_BYTES,
    )
    receipts = _receipts(api, snapshot, child_request_id)
    if record is None:
        if receipts:
            raise ValueError("Private browser v2 orphan receipts without binding")
        return {
            "source_head_sha": snapshot.head, "child_request_id": child_request_id,
            "status": "absent_no_send", "browser_send_authorized": False,
        }
    declared_origin = record.get("binding_origin_head_sha")
    if not isinstance(declared_origin, str):
        raise ValueError("Private browser binding origin missing")
    binding.validate_binding(
        record, parent, snapshot.dispatch,
        expected_origin_head_sha=declared_origin,
    )
    lineage = _proven_lineage(api, snapshot.head, declared_origin, path, record)
    _verify_receipt_history(
        api, declared_origin, lineage, dispatch_id, child_request_id, receipts,
    )
    _verify_origin(api, declared_origin, dispatch_id, record, snapshot.dispatch)
    projection = binding.receipt_projection(
        parent, snapshot.dispatch, record, receipts,
        expected_origin_head_sha=declared_origin,
    )
    _reject_sibling_reuse(api, snapshot, record, receipts)
    return {
        "source_head_sha": snapshot.head,
        "binding_origin_head_sha": declared_origin,
        "parent_id": record["parent_id"],
        "child_request_id": child_request_id,
        "status": projection["state"],
        "requires_reconciliation": projection["requires_reconciliation"],
        "browser_send_authorized": False,
    }
