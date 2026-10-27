from __future__ import annotations

import hashlib
import json
import re
import secrets
from typing import Any

from local_agent.conversation.contract import (
    BOOTSTRAP_CONTRACT_VERSION,
    child_request_digest,
    validate_child_request,
)

MAX_CHILD_BOOTSTRAP_BYTES = 48 * 1024

_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_BOOTSTRAP_INSTRUCTIONS = (
    "Treat this bootstrap as the complete admitted startup authority for this child conversation.",
    "Work only from the immutable request fields and content-addressed context references below.",
    "Repository identity and binding fields describe admitted reasoning provenance/context; they do not grant host or repository execution authority.",
    "Any executable repository work must be delegated through a separate Local Agent .agent/tasks request using the exact canonical binding of the actual target repository.",
    "Do not substitute browser state, a moved repository ref, or unreferenced transcript history for admitted authority.",
)


def _identity_request(request: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in request.items() if key != "created_at"}


def child_bootstrap_record(request: dict[str, Any]) -> dict[str, Any]:
    validate_child_request(request)
    return {
        "bootstrap_contract_version": BOOTSTRAP_CONTRACT_VERSION,
        "child_request_digest": child_request_digest(request),
        "instructions": list(_BOOTSTRAP_INSTRUCTIONS),
        "request": _identity_request(request),
    }


def child_bootstrap_message(request: dict[str, Any]) -> str:
    record = child_bootstrap_record(request)
    body = json.dumps(
        record,
        sort_keys=True,
        ensure_ascii=False,
        indent=2,
        allow_nan=False,
    )
    message = f"LOCAL AGENT CHILD BOOTSTRAP\n{body}\n"
    if len(message.encode("utf-8")) > MAX_CHILD_BOOTSTRAP_BYTES:
        raise ValueError(f"child bootstrap exceeds {MAX_CHILD_BOOTSTRAP_BYTES} bytes")
    return message


def child_bootstrap_digest(request: dict[str, Any]) -> str:
    message = child_bootstrap_message(request)
    return "sha256:" + hashlib.sha256(message.encode("utf-8")).hexdigest()


def require_child_bootstrap_digest(
    request: dict[str, Any], *, expected_digest: str
) -> None:
    if not isinstance(expected_digest, str) or not _DIGEST_RE.fullmatch(expected_digest):
        raise ValueError("expected child bootstrap digest must be a canonical sha256 digest")
    actual = child_bootstrap_digest(request)
    if not secrets.compare_digest(actual, expected_digest):
        raise ValueError(
            f"child bootstrap digest mismatch: expected {expected_digest}, got {actual}"
        )
