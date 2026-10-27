"""Disabled-by-default synthetic GitHub Fabric publication preflight.

This planner performs no GitHub, browser, or local filesystem writes. Its one
fixed synthetic fixture is intentionally public; arbitrary admitted requests
must never be released through the public chat-bridge-state raw endpoint.
A future trusted writer must fetch records from the derived paths and enforce
the returned head SHA as a compare-and-swap lease before each write.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from dataclasses import dataclass
from typing import Any, Iterable

from local_agent.conversation.github_fabric_dispatch import (
    build_github_fabric_dispatch,
    reconcile_github_fabric_dispatch,
)

INDEX_PATH = ".agent/conversation/browser_dispatches/index.json"
RECORD_ROOT = ".agent/conversation/browser_dispatches/"
INDEX_SCHEMA_VERSION = 1
MAX_INDEX_IDS = 4
MAX_INDEX_BYTES = 8192
MAX_PREFLIGHT_INPUT_BYTES = 128 * 1024
_DISPATCH_ID = re.compile(r"fabric-[0-9a-f]{32}\Z")
_HEAD_SHA = re.compile(r"[0-9a-f]{40}\Z")

# Exact canonical SHA-256 of the deliberately public, synthetic Python test
# fixture in tests/test_github_fabric_dispatch.py (operator + admitted children).
# Neither a caller-provided 'synthetic' boolean nor a supplied digest is proof
# that potentially private bootstrap text is safe to publish.
_APPROVED_SYNTHETIC_SOURCE_SHA256 = (
    "89c420c4918f253be18da93e7ae54f216f7dc49f4e110c328f0ccd7e8f442888"
)


@dataclass(frozen=True, slots=True)
class SyntheticPublicationStep:
    operation: str
    path: str | None
    payload: dict[str, Any] | None
    expected_head_sha: str


def _canonical_bytes(value: Any) -> bytes:
    try:
        return json.dumps(
            value, ensure_ascii=False, sort_keys=True,
            separators=(",", ":"), allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ValueError("GitHub Fabric publication requires canonical JSON") from exc


def _validate_index(index: Any) -> dict[str, Any]:
    if index is None:
        return {"schema_version": INDEX_SCHEMA_VERSION, "dispatch_ids": []}
    if not isinstance(index, dict) or set(index) != {"schema_version", "dispatch_ids"}:
        raise ValueError("GitHub Fabric publication index fields are invalid")
    ids = index.get("dispatch_ids")
    if (
        type(index["schema_version"]) is not int
        or index["schema_version"] != INDEX_SCHEMA_VERSION
        or not isinstance(ids, list)
        or len(ids) > MAX_INDEX_IDS
        or any(not isinstance(value, str) or not _DISPATCH_ID.fullmatch(value) for value in ids)
        or len(set(ids)) != len(ids)
        or len(_canonical_bytes(index)) > MAX_INDEX_BYTES
    ):
        raise ValueError("GitHub Fabric publication index is invalid or exceeds capacity")
    return {"schema_version": INDEX_SCHEMA_VERSION, "dispatch_ids": list(ids)}


def preflight_synthetic_publication(
    operator_request: dict[str, Any],
    child_requests: Iterable[dict[str, Any]],
    *,
    existing_record: dict[str, Any] | None,
    existing_index: dict[str, Any] | None,
    expected_head_sha: str,
    enabled: bool = False,
    writer_authorized: bool = False,
) -> SyntheticPublicationStep:
    """Plan at most one operation from a freshly inspected remote snapshot.

    The caller must separately prove snapshot origin and enforce an exact-head
    lease for the returned operation. This preflight cannot authorize any write.
    A lost response or failed write requires a fresh snapshot and a new plan.
    """
    if enabled is not True or writer_authorized is not True:
        raise PermissionError("GitHub Fabric synthetic publication is disabled")
    if not isinstance(expected_head_sha, str) or not _HEAD_SHA.fullmatch(expected_head_sha):
        raise ValueError("GitHub Fabric publication requires an exact remote head SHA")

    requests = list(child_requests)
    if not isinstance(operator_request, dict) or not all(
        isinstance(request, dict) and isinstance(request.get("id"), str)
        for request in requests
    ):
        raise ValueError("GitHub Fabric publication source is invalid")
    source = {
        "operator_request": operator_request,
        "child_requests": sorted(requests, key=lambda request: request["id"]),
    }
    encoded = _canonical_bytes(source)
    if len(encoded) > MAX_PREFLIGHT_INPUT_BYTES:
        raise ValueError("GitHub Fabric publication source exceeds bound")
    fingerprint = hashlib.sha256(encoded).hexdigest()
    if not hmac.compare_digest(fingerprint, _APPROVED_SYNTHETIC_SOURCE_SHA256):
        raise PermissionError("GitHub Fabric publication is restricted to the exact synthetic fixture")

    dispatch = build_github_fabric_dispatch(operator_request, requests)
    if not _DISPATCH_ID.fullmatch(dispatch["id"]):
        raise ValueError("GitHub Fabric publication dispatch identity is invalid")
    index = _validate_index(existing_index)
    record_path = RECORD_ROOT + dispatch["id"] + ".json"

    if existing_record is None:
        if dispatch["id"] in index["dispatch_ids"]:
            raise ValueError("GitHub Fabric index refers to a missing immutable record")
        if len(index["dispatch_ids"]) >= MAX_INDEX_IDS:
            raise ValueError("GitHub Fabric publication index capacity exhausted")
        return SyntheticPublicationStep("create_record", record_path, dispatch, expected_head_sha)

    reconcile_github_fabric_dispatch(existing_record, dispatch)
    if dispatch["id"] in index["dispatch_ids"]:
        return SyntheticPublicationStep("replay", None, None, expected_head_sha)
    if len(index["dispatch_ids"]) >= MAX_INDEX_IDS:
        raise ValueError("GitHub Fabric publication index capacity exhausted")
    updated = {
        "schema_version": INDEX_SCHEMA_VERSION,
        "dispatch_ids": [*index["dispatch_ids"], dispatch["id"]],
    }
    _validate_index(updated)
    return SyntheticPublicationStep("update_index", INDEX_PATH, updated, expected_head_sha)
