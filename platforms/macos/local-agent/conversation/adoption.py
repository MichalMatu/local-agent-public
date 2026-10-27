from __future__ import annotations

import hashlib
import json
import re
import secrets
from datetime import datetime, timezone
from typing import Any

from local_agent.conversation import contract, terminal

ADOPTION_RECORD_SCHEMA_VERSION = 1
MAX_ADOPTION_RECORD_BYTES = 16 * 1024

_ID_RE = re.compile(r"^[A-Za-z0-9._-]+$")
_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")

_ADOPTION_RECORD_FIELDS = frozenset(
    {
        "schema_version",
        "child_request_id",
        "child_request_digest",
        "terminal_record_digest",
        "workflow_id",
        "workflow_node_id",
        "adopted_at",
    }
)


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
        raise ValueError("adoption record must be canonical JSON data") from exc
    return text.encode("utf-8")


def _validate_id(value: Any, *, field: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > contract.MAX_ID_CHARS
        or value != value.strip()
        or not _ID_RE.fullmatch(value)
    ):
        raise ValueError(
            f"{field} must be a canonical non-empty identifier up to "
            f"{contract.MAX_ID_CHARS} characters"
        )
    return value


def _validate_digest(value: Any, *, field: str) -> str:
    if not isinstance(value, str) or not _DIGEST_RE.fullmatch(value):
        raise ValueError(f"{field} must be a canonical sha256 digest")
    return value


def _validate_timestamp(value: Any, *, field: str) -> str:
    if not isinstance(value, str) or not value.endswith("Z") or len(value) > 64:
        raise ValueError(f"{field} must be a bounded RFC3339 UTC timestamp ending in Z")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise ValueError(f"{field} must be a valid RFC3339 UTC timestamp") from exc
    if parsed.tzinfo != timezone.utc:
        raise ValueError(f"{field} must use UTC")
    return value


def terminal_record_digest(
    record: dict[str, Any],
    *,
    request: dict[str, Any],
    registration: dict[str, Any],
) -> str:
    terminal.validate_terminal_record(
        record,
        request=request,
        registration=registration,
    )
    return "sha256:" + hashlib.sha256(_canonical_bytes(record)).hexdigest()


def validate_adoption_record(
    record: dict[str, Any],
    *,
    request: dict[str, Any] | None = None,
    registration: dict[str, Any] | None = None,
    terminal_record: dict[str, Any] | None = None,
) -> None:
    if not isinstance(record, dict):
        raise ValueError("child adoption record must be an object")
    if len(_canonical_bytes(record)) > MAX_ADOPTION_RECORD_BYTES:
        raise ValueError(f"child adoption record exceeds {MAX_ADOPTION_RECORD_BYTES} bytes")
    if set(record) != _ADOPTION_RECORD_FIELDS:
        raise ValueError("child adoption record fields do not match schema")
    if (
        type(record.get("schema_version")) is not int
        or record["schema_version"] != ADOPTION_RECORD_SCHEMA_VERSION
    ):
        raise ValueError(
            f"child adoption record schema_version must be {ADOPTION_RECORD_SCHEMA_VERSION}"
        )

    _validate_id(record.get("child_request_id"), field="child_request_id")
    _validate_digest(record.get("child_request_digest"), field="child_request_digest")
    _validate_digest(record.get("terminal_record_digest"), field="terminal_record_digest")
    _validate_id(record.get("workflow_id"), field="workflow_id")
    _validate_id(record.get("workflow_node_id"), field="workflow_node_id")
    _validate_timestamp(record.get("adopted_at"), field="adoption adopted_at")

    if request is not None:
        contract.validate_child_request(request)
        if record["child_request_id"] != request["id"]:
            raise ValueError("adoption record request id does not match admitted request")
        expected_request_digest = contract.child_request_digest(request)
        if not secrets.compare_digest(
            str(record["child_request_digest"]),
            expected_request_digest,
        ):
            raise ValueError("adoption record request digest does not match admitted request")
        if record["workflow_id"] != request["workflow_id"]:
            raise ValueError("adoption record workflow id does not match admitted request")
        if record["workflow_node_id"] != request["workflow_node_id"]:
            raise ValueError("adoption record workflow node does not match admitted request")

    if terminal_record is not None:
        if request is None or registration is None:
            raise ValueError(
                "request and registration are required to validate adopted terminal record"
            )
        expected_terminal_digest = terminal_record_digest(
            terminal_record,
            request=request,
            registration=registration,
        )
        if not secrets.compare_digest(
            str(record["terminal_record_digest"]),
            expected_terminal_digest,
        ):
            raise ValueError(
                "adoption record terminal digest does not match durable terminal record"
            )


def reconcile_adoption_record(
    existing: dict[str, Any] | None,
    candidate: dict[str, Any],
    *,
    request: dict[str, Any],
    registration: dict[str, Any],
    terminal_record: dict[str, Any],
) -> dict[str, Any]:
    validate_adoption_record(
        candidate,
        request=request,
        registration=registration,
        terminal_record=terminal_record,
    )
    if existing is None:
        return dict(candidate)

    validate_adoption_record(
        existing,
        request=request,
        registration=registration,
        terminal_record=terminal_record,
    )
    for field, message in (
        ("child_request_id", "conflicting adoption request id"),
        ("child_request_digest", "conflicting adoption request digest"),
        ("terminal_record_digest", "conflicting adopted terminal digest"),
        ("workflow_id", "conflicting adoption workflow id"),
        ("workflow_node_id", "conflicting adoption workflow node"),
    ):
        if candidate[field] != existing[field]:
            raise ValueError(message)
    return dict(existing)
