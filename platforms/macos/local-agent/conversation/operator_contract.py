"""Bounded GitHub-facing operator contract for Conversation Fabric delegation."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

from local_agent.conversation import contract

LEGACY_OPERATOR_REQUEST_SCHEMA_VERSION = 1
TARGETED_OPERATOR_REQUEST_SCHEMA_VERSION = 2
OPERATOR_REQUEST_SCHEMA_VERSION = 3
OPERATOR_RESULT_SCHEMA_VERSION = 1
MAX_OPERATOR_REQUEST_BYTES = 128 * 1024
MAX_OPERATOR_RESULT_BYTES = 64 * 1024
MAX_OPERATOR_CHILDREN = 4
MAX_OPERATOR_SUMMARY_CHARS = 4096
MAX_OPERATOR_ERROR_CHARS = 1024
MAX_OPERATOR_PATHS = 64
MAX_OPERATOR_PATH_CHARS = 1024
MAX_OPERATOR_ID_CHARS = 120
MAX_OPERATOR_REPOSITORY_ID_CHARS = 120
MAX_OPERATOR_REPOSITORY_IDS = 16

_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,119}$")
_REPOSITORY_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,119}$")
_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")


def _canonical_bytes(payload: Any) -> bytes:
    try:
        text = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("operator payload must be canonical JSON data") from exc
    return text.encode("utf-8")


def _bounded_id(value: Any, *, field: str) -> str:
    if not isinstance(value, str) or not _ID_RE.fullmatch(value):
        raise ValueError(f"{field} must be a bounded canonical id")
    return value


def _repository_id(value: Any) -> str:
    if (
        not isinstance(value, str)
        or len(value) > MAX_OPERATOR_REPOSITORY_ID_CHARS
        or not _REPOSITORY_ID_RE.fullmatch(value)
    ):
        raise ValueError("operator repository_id must be a bounded canonical repository id")
    return value


def _repository_ids(value: Any) -> list[str]:
    if not isinstance(value, list) or not 1 <= len(value) <= MAX_OPERATOR_REPOSITORY_IDS:
        raise ValueError(
            f"operator repository_ids must contain 1..{MAX_OPERATOR_REPOSITORY_IDS} items"
        )
    result: list[str] = []
    seen: set[str] = set()
    for raw in value:
        repository_id = _repository_id(raw)
        key = repository_id.casefold()
        if key in seen:
            raise ValueError(f"duplicate operator repository_id: {repository_id!r}")
        seen.add(key)
        result.append(repository_id)
    return result


def _reasoning_scope_summary(summary: str, repository_ids: list[str]) -> str:
    if not repository_ids:
        return summary
    return (
        "Reasoning repository context (not execution authority): "
        + ", ".join(repository_ids)
        + ". "
        + summary
    )


def validate_operator_request(request: dict[str, Any]) -> None:
    if not isinstance(request, dict):
        raise ValueError("operator request must be an object")
    if len(_canonical_bytes(request)) > MAX_OPERATOR_REQUEST_BYTES:
        raise ValueError(f"operator request exceeds {MAX_OPERATOR_REQUEST_BYTES} bytes")

    schema_version = request.get("schema_version")
    repository_ids: list[str] = []
    if schema_version == LEGACY_OPERATOR_REQUEST_SCHEMA_VERSION:
        required = {
            "schema_version",
            "id",
            "workflow_id",
            "parent_conversation_url",
            "children",
        }
    elif schema_version == TARGETED_OPERATOR_REQUEST_SCHEMA_VERSION:
        required = {
            "schema_version",
            "id",
            "workflow_id",
            "parent_conversation_url",
            "repository_id",
            "children",
        }
        repository_ids = [_repository_id(request.get("repository_id"))]
    elif schema_version == OPERATOR_REQUEST_SCHEMA_VERSION:
        required = {
            "schema_version",
            "id",
            "workflow_id",
            "parent_conversation_url",
            "repository_ids",
            "children",
        }
        repository_ids = _repository_ids(request.get("repository_ids"))
    else:
        raise ValueError(
            "operator request schema_version must be "
            f"{LEGACY_OPERATOR_REQUEST_SCHEMA_VERSION}, "
            f"{TARGETED_OPERATOR_REQUEST_SCHEMA_VERSION}, or {OPERATOR_REQUEST_SCHEMA_VERSION}"
        )

    if set(request) != required:
        raise ValueError("operator request fields do not match schema")
    _bounded_id(request.get("id"), field="operator request id")
    _bounded_id(request.get("workflow_id"), field="operator workflow id")
    parent = contract.canonical_conversation_url(request.get("parent_conversation_url"))
    if parent != request.get("parent_conversation_url"):
        raise ValueError("operator parent_conversation_url must be canonical")

    children = request.get("children")
    if not isinstance(children, list) or not 1 <= len(children) <= MAX_OPERATOR_CHILDREN:
        raise ValueError(f"operator children must contain 1..{MAX_OPERATOR_CHILDREN} items")
    child_fields = {"request_id", "node_id", "role", "summary", "paths"}
    seen_requests: set[str] = set()
    seen_nodes: set[str] = set()
    for child in children:
        if not isinstance(child, dict) or set(child) != child_fields:
            raise ValueError("operator child fields do not match schema")
        request_id = _bounded_id(child.get("request_id"), field="operator child request_id")
        node_id = _bounded_id(child.get("node_id"), field="operator child node_id")
        if request_id in seen_requests or node_id in seen_nodes:
            raise ValueError("operator child request_id and node_id values must be unique")
        seen_requests.add(request_id)
        seen_nodes.add(node_id)
        if child.get("role") not in contract.CHILD_ROLES:
            raise ValueError(
                f"operator child role must be one of {sorted(contract.CHILD_ROLES)!r}"
            )
        summary = child.get("summary")
        if (
            not isinstance(summary, str)
            or not summary.strip()
            or len(summary) > MAX_OPERATOR_SUMMARY_CHARS
        ):
            raise ValueError("operator child summary must be a bounded non-empty string")
        if len(_reasoning_scope_summary(summary, repository_ids)) > contract.MAX_SCOPE_SUMMARY_CHARS:
            raise ValueError("operator child summary plus repository context exceeds child scope bound")
        paths = child.get("paths")
        if not isinstance(paths, list) or not 1 <= len(paths) <= MAX_OPERATOR_PATHS:
            raise ValueError("operator child paths must be a bounded non-empty list")
        for item in paths:
            if (
                not isinstance(item, str)
                or not item.strip()
                or len(item) > MAX_OPERATOR_PATH_CHARS
            ):
                raise ValueError("operator child path must be a bounded non-empty string")


def operator_request_repository_ids(request: dict[str, Any]) -> list[str]:
    """Return repository names as non-authoritative reasoning context."""
    validate_operator_request(request)
    if request["schema_version"] == LEGACY_OPERATOR_REQUEST_SCHEMA_VERSION:
        return []
    if request["schema_version"] == TARGETED_OPERATOR_REQUEST_SCHEMA_VERSION:
        return [str(request["repository_id"])]
    return list(request["repository_ids"])


def operator_request_repository_id(request: dict[str, Any]) -> str | None:
    """Compatibility helper for one-repository reasoning requests."""
    repository_ids = operator_request_repository_ids(request)
    return repository_ids[0] if len(repository_ids) == 1 else None


def operator_request_digest(request: dict[str, Any]) -> str:
    validate_operator_request(request)
    return "sha256:" + hashlib.sha256(_canonical_bytes(request)).hexdigest()


def request_to_mvp_spec(request: dict[str, Any]) -> dict[str, Any]:
    """Map repository names into child prose without granting execution authority."""
    validate_operator_request(request)
    repository_ids = operator_request_repository_ids(request)
    children: list[dict[str, Any]] = []
    for raw in request["children"]:
        child = dict(raw)
        child["summary"] = _reasoning_scope_summary(str(child["summary"]), repository_ids)
        children.append(child)
    return {
        "schema_version": 1,
        "workflow_id": request["workflow_id"],
        "parent_conversation_url": request["parent_conversation_url"],
        "children": children,
    }


def load_operator_request(path: Path) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"operator request is unavailable or unsafe: {path}")
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise ValueError(f"operator request is unavailable: {path}") from exc
    if len(raw) > MAX_OPERATOR_REQUEST_BYTES:
        raise ValueError(f"operator request exceeds {MAX_OPERATOR_REQUEST_BYTES} bytes")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"operator request is invalid JSON: {path}") from exc
    validate_operator_request(payload)
    return payload


def _bounded_text(value: Any, limit: int) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    return text[:limit]


def _operator_child_state(child: dict[str, Any]) -> str:
    if child.get("error"):
        return "failed"
    lifecycle = str(child.get("child_state") or "")
    if lifecycle == "retired":
        return "completed"
    if lifecycle in {"abandoned", "cancelled"}:
        return "failed"
    if lifecycle in {
        "requested",
        "registration_pending",
        "active",
        "terminal_pending_evidence",
        "terminal_recorded",
    }:
        return "waiting"
    return "failed" if child.get("error") else "waiting"


def build_operator_result(
    request: dict[str, Any],
    campaign: dict[str, Any],
) -> dict[str, Any]:
    """Project internal campaign details into one bounded operator-facing snapshot."""
    validate_operator_request(request)
    if not isinstance(campaign, dict):
        raise ValueError("campaign result must be an object")
    raw_children = campaign.get("children")
    if not isinstance(raw_children, list):
        raise ValueError("campaign result children must be a list")
    expected_ids = [str(child["request_id"]) for child in request["children"]]
    by_id: dict[str, dict[str, Any]] = {}
    for child in raw_children:
        if not isinstance(child, dict):
            raise ValueError("campaign child result must be an object")
        request_id = child.get("request_id")
        if not isinstance(request_id, str) or request_id in by_id:
            raise ValueError("campaign child result identity is invalid")
        by_id[request_id] = child
    if set(by_id) != set(expected_ids):
        raise ValueError("campaign child results do not match operator request")

    children: list[dict[str, Any]] = []
    for request_id in expected_ids:
        child = by_id[request_id]
        state = _operator_child_state(child)
        children.append(
            {
                "request_id": request_id,
                "state": state,
                "summary": (
                    _bounded_text(child.get("summary"), MAX_OPERATOR_SUMMARY_CHARS)
                    if state == "completed"
                    else None
                ),
                "error": (
                    _bounded_text(child.get("error"), MAX_OPERATOR_ERROR_CHARS)
                    if state != "completed"
                    else None
                ),
                "child_conversation_url": child.get("child_conversation_url"),
                "evidence_digest": child.get("evidence_digest"),
            }
        )

    completed = sum(item["state"] == "completed" for item in children)
    failed = sum(item["state"] == "failed" for item in children)
    waiting = sum(item["state"] == "waiting" for item in children)
    state = "completed" if completed == len(children) else "waiting" if waiting else "failed"
    result = {
        "schema_version": OPERATOR_RESULT_SCHEMA_VERSION,
        "request_id": request["id"],
        "request_digest": operator_request_digest(request),
        "workflow_id": request["workflow_id"],
        "state": state,
        "children_total": len(children),
        "children_completed": completed,
        "children_failed": failed,
        "children_waiting": waiting,
        "children": children,
    }
    validate_operator_result(result, request)
    return result


def validate_operator_result(result: dict[str, Any], request: dict[str, Any]) -> None:
    validate_operator_request(request)
    if not isinstance(result, dict) or len(_canonical_bytes(result)) > MAX_OPERATOR_RESULT_BYTES:
        raise ValueError("operator result is invalid or oversized")
    required = {
        "schema_version",
        "request_id",
        "request_digest",
        "workflow_id",
        "state",
        "children_total",
        "children_completed",
        "children_failed",
        "children_waiting",
        "children",
    }
    if set(result) != required:
        raise ValueError("operator result fields do not match schema")
    if result.get("schema_version") != OPERATOR_RESULT_SCHEMA_VERSION:
        raise ValueError("operator result schema version mismatch")
    if result.get("request_id") != request["id"]:
        raise ValueError("operator result request id mismatch")
    if result.get("request_digest") != operator_request_digest(request):
        raise ValueError("operator result request digest mismatch")
    if result.get("workflow_id") != request["workflow_id"]:
        raise ValueError("operator result workflow id mismatch")
    if result.get("state") not in {"completed", "waiting", "failed"}:
        raise ValueError("operator result state is invalid")

    children = result.get("children")
    if not isinstance(children, list) or len(children) != len(request["children"]):
        raise ValueError("operator result children are invalid")
    expected_ids = [str(item["request_id"]) for item in request["children"]]
    observed_ids: list[str] = []
    counts = {"completed": 0, "waiting": 0, "failed": 0}
    child_fields = {
        "request_id",
        "state",
        "summary",
        "error",
        "child_conversation_url",
        "evidence_digest",
    }
    for child in children:
        if not isinstance(child, dict) or set(child) != child_fields:
            raise ValueError("operator result child fields do not match schema")
        request_id = child.get("request_id")
        state = child.get("state")
        if not isinstance(request_id, str) or state not in counts:
            raise ValueError("operator result child identity/state is invalid")
        observed_ids.append(request_id)
        counts[state] += 1
        summary = child.get("summary")
        error = child.get("error")
        if summary is not None and (
            not isinstance(summary, str) or len(summary) > MAX_OPERATOR_SUMMARY_CHARS
        ):
            raise ValueError("operator result child summary is invalid")
        if error is not None and (
            not isinstance(error, str) or len(error) > MAX_OPERATOR_ERROR_CHARS
        ):
            raise ValueError("operator result child error is invalid")
        if state == "completed" and (not isinstance(summary, str) or not summary):
            raise ValueError("completed operator child requires a summary")
        if state == "completed" and error is not None:
            raise ValueError("completed operator child must not include an error")
        child_url = child.get("child_conversation_url")
        if child_url is not None and contract.canonical_conversation_url(child_url) != child_url:
            raise ValueError("operator result child conversation URL is invalid")
        evidence_digest = child.get("evidence_digest")
        if evidence_digest is not None and (
            not isinstance(evidence_digest, str) or not _DIGEST_RE.fullmatch(evidence_digest)
        ):
            raise ValueError("operator result child evidence digest is invalid")

    if observed_ids != expected_ids:
        raise ValueError("operator result child ordering/identity mismatch")
    if result.get("children_total") != len(children):
        raise ValueError("operator result child total mismatch")
    if result.get("children_completed") != counts["completed"]:
        raise ValueError("operator result completed count mismatch")
    if result.get("children_failed") != counts["failed"]:
        raise ValueError("operator result failed count mismatch")
    if result.get("children_waiting") != counts["waiting"]:
        raise ValueError("operator result waiting count mismatch")
    expected_state = (
        "completed"
        if counts["completed"] == len(children)
        else "waiting"
        if counts["waiting"]
        else "failed"
    )
    if result.get("state") != expected_state:
        raise ValueError("operator result aggregate state mismatch")


def operator_result_bytes(result: dict[str, Any], request: dict[str, Any]) -> bytes:
    validate_operator_result(result, request)
    return json.dumps(result, indent=2, ensure_ascii=False, sort_keys=True).encode("utf-8") + b"\n"
