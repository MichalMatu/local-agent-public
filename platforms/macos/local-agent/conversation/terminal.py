from __future__ import annotations

import hashlib
import json
import re
import secrets
from datetime import datetime, timezone
from typing import Any

from local_agent.conversation import contract

TERMINAL_RECORD_SCHEMA_VERSION = 1
MAX_TERMINAL_RECORD_BYTES = 32 * 1024
MAX_TERMINAL_SUMMARY_CHARS = 4096
MAX_TERMINAL_EVIDENCE_REFS = 64

_ID_RE = re.compile(r"^[A-Za-z0-9._-]+$")
_KIND_RE = re.compile(r"^[a-z][a-z0-9._-]*$")
_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")

_TERMINAL_RECORD_FIELDS = frozenset(
    {
        "schema_version",
        "child_request_id",
        "child_request_digest",
        "child_registration_digest",
        "child_conversation_url",
        "summary",
        "evidence_refs",
        "recorded_at",
    }
)
_EVIDENCE_REF_FIELDS = frozenset({"kind", "id", "digest"})


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
        raise ValueError("terminal record must be canonical JSON data") from exc
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


def child_registration_digest(
    registration: dict[str, Any],
    *,
    request: dict[str, Any] | None = None,
) -> str:
    contract.validate_child_registration(registration, request=request)
    return "sha256:" + hashlib.sha256(_canonical_bytes(registration)).hexdigest()


def _validate_evidence_refs(value: Any) -> None:
    if not isinstance(value, list) or not value:
        raise ValueError("terminal evidence_refs must be a non-empty list")
    if len(value) > MAX_TERMINAL_EVIDENCE_REFS:
        raise ValueError(
            f"terminal evidence_refs exceeds {MAX_TERMINAL_EVIDENCE_REFS} items"
        )
    seen: set[tuple[str, str]] = set()
    for raw in value:
        if not isinstance(raw, dict) or set(raw) != _EVIDENCE_REF_FIELDS:
            raise ValueError("terminal evidence reference fields do not match schema")
        kind = raw.get("kind")
        if (
            not isinstance(kind, str)
            or not kind
            or len(kind) > contract.MAX_CONTEXT_KIND_CHARS
            or kind != kind.strip()
            or kind != kind.casefold()
            or not _KIND_RE.fullmatch(kind)
        ):
            raise ValueError(
                "terminal evidence reference kind must be a canonical lowercase identifier"
            )
        reference_id = _validate_id(raw.get("id"), field="terminal evidence reference id")
        _validate_digest(raw.get("digest"), field="terminal evidence reference digest")
        key = (kind, reference_id)
        if key in seen:
            raise ValueError(f"duplicate terminal evidence reference: {kind}:{reference_id}")
        seen.add(key)


def validate_terminal_record(
    record: dict[str, Any],
    *,
    request: dict[str, Any] | None = None,
    registration: dict[str, Any] | None = None,
) -> None:
    if not isinstance(record, dict):
        raise ValueError("child terminal record must be an object")
    if len(_canonical_bytes(record)) > MAX_TERMINAL_RECORD_BYTES:
        raise ValueError(f"child terminal record exceeds {MAX_TERMINAL_RECORD_BYTES} bytes")
    if set(record) != _TERMINAL_RECORD_FIELDS:
        raise ValueError("child terminal record fields do not match schema")
    if (
        type(record.get("schema_version")) is not int
        or record["schema_version"] != TERMINAL_RECORD_SCHEMA_VERSION
    ):
        raise ValueError(
            f"child terminal record schema_version must be {TERMINAL_RECORD_SCHEMA_VERSION}"
        )

    _validate_id(record.get("child_request_id"), field="child_request_id")
    _validate_digest(record.get("child_request_digest"), field="child_request_digest")
    _validate_digest(
        record.get("child_registration_digest"),
        field="child_registration_digest",
    )
    canonical_child_url = contract.canonical_conversation_url(
        record.get("child_conversation_url")
    )
    if record["child_conversation_url"] != canonical_child_url:
        raise ValueError("child_conversation_url must use canonical ChatGPT URL form")

    summary = record.get("summary")
    if (
        not isinstance(summary, str)
        or not summary
        or summary != summary.strip()
        or len(summary) > MAX_TERMINAL_SUMMARY_CHARS
    ):
        raise ValueError("terminal summary must be a non-empty bounded string")
    _validate_evidence_refs(record.get("evidence_refs"))
    _validate_timestamp(record.get("recorded_at"), field="terminal recorded_at")

    if request is not None:
        contract.validate_child_request(request)
        if record["child_request_id"] != request["id"]:
            raise ValueError("terminal record request id does not match admitted request")
        expected_request_digest = contract.child_request_digest(request)
        if not secrets.compare_digest(
            str(record["child_request_digest"]), expected_request_digest
        ):
            raise ValueError("terminal record request digest does not match admitted request")

    if registration is not None:
        contract.validate_child_registration(registration, request=request)
        if record["child_request_id"] != registration["child_request_id"]:
            raise ValueError("terminal record request id does not match child registration")
        if not secrets.compare_digest(
            str(record["child_request_digest"]),
            str(registration["child_request_digest"]),
        ):
            raise ValueError("terminal record request digest does not match child registration")
        expected_registration_digest = child_registration_digest(
            registration,
            request=request,
        )
        if not secrets.compare_digest(
            str(record["child_registration_digest"]),
            expected_registration_digest,
        ):
            raise ValueError("terminal record registration digest does not match registration")
        if record["child_conversation_url"] != registration["child_conversation_url"]:
            raise ValueError("terminal record child conversation does not match registration")


def reconcile_terminal_record(
    existing: dict[str, Any] | None,
    candidate: dict[str, Any],
    *,
    request: dict[str, Any],
    registration: dict[str, Any],
) -> dict[str, Any]:
    validate_terminal_record(candidate, request=request, registration=registration)
    if existing is None:
        return dict(candidate)

    validate_terminal_record(existing, request=request, registration=registration)
    for field, message in (
        ("child_request_id", "conflicting terminal record request id"),
        ("child_request_digest", "conflicting terminal record request digest"),
        ("child_registration_digest", "conflicting terminal registration digest"),
        ("child_conversation_url", "conflicting terminal child conversation URL"),
        ("summary", "conflicting terminal summary"),
        ("evidence_refs", "conflicting terminal evidence references"),
    ):
        if candidate[field] != existing[field]:
            raise ValueError(message)
    return dict(existing)
