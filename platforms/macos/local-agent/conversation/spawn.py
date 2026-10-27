from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any

from local_agent.conversation import bootstrap, contract

SPAWN_TRANSACTION_SCHEMA_VERSION = 1
MAX_SPAWN_TRANSACTION_BYTES = 16 * 1024
MAX_SPAWN_ATTEMPTS = 32
MAX_SPAWN_REASON_CHARS = 2048

SPAWN_STATES = frozenset(
    {
        "pending",
        "tab_created",
        "bootstrap_ready",
        "bootstrap_submitting",
        "identity_discovered",
        "registration_submitting",
        "done",
        "failed",
        "ambiguous",
        "cancelled",
    }
)
TERMINAL_SPAWN_STATES = frozenset({"done", "failed", "ambiguous", "cancelled"})
BROWSER_ACTIVE_SPAWN_STATES = frozenset(
    {
        "tab_created",
        "bootstrap_ready",
        "bootstrap_submitting",
        "identity_discovered",
        "registration_submitting",
    }
)
SAFE_PRE_SUBMIT_STATES = frozenset({"pending", "tab_created", "bootstrap_ready"})
SIDE_EFFECT_POSSIBLE_STATES = frozenset(
    {"bootstrap_submitting", "identity_discovered", "registration_submitting"}
)

_ALLOWED_TRANSITIONS: dict[str, frozenset[str]] = {
    "pending": frozenset({"tab_created", "failed", "cancelled"}),
    "tab_created": frozenset({"bootstrap_ready", "failed", "cancelled"}),
    "bootstrap_ready": frozenset({"bootstrap_submitting", "failed", "cancelled"}),
    "bootstrap_submitting": frozenset({"identity_discovered", "ambiguous"}),
    "identity_discovered": frozenset({"registration_submitting", "ambiguous"}),
    "registration_submitting": frozenset({"done", "ambiguous"}),
    "done": frozenset(),
    "failed": frozenset(),
    "ambiguous": frozenset(),
    "cancelled": frozenset(),
}

_TRANSACTION_FIELDS = frozenset(
    {
        "schema_version",
        "id",
        "child_request_id",
        "child_request_digest",
        "bootstrap_digest",
        "attempt",
        "state",
        "tab_id",
        "child_conversation_url",
        "failure_reason",
        "created_at",
        "updated_at",
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
        raise ValueError("spawn transaction must be canonical JSON data") from exc
    return text.encode("utf-8")


def _validate_attempt(value: Any) -> int:
    if type(value) is not int or value < 1 or value > MAX_SPAWN_ATTEMPTS:
        raise ValueError(f"spawn attempt must be 1..{MAX_SPAWN_ATTEMPTS}")
    return value


def _parse_timestamp(value: Any, *, field: str) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z") or len(value) > 64:
        raise ValueError(f"{field} must be a bounded RFC3339 UTC timestamp ending in Z")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise ValueError(f"{field} must be a valid RFC3339 UTC timestamp") from exc
    if parsed.tzinfo != timezone.utc:
        raise ValueError(f"{field} must use UTC")
    return parsed


def _validate_reason(value: Any, *, required: bool) -> str | None:
    if value is None and not required:
        return None
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > MAX_SPAWN_REASON_CHARS
    ):
        raise ValueError("spawn failure_reason must be a non-empty bounded string")
    return value


def spawn_transaction_id(request: dict[str, Any], attempt: int) -> str:
    contract.validate_child_request(request)
    canonical_attempt = _validate_attempt(attempt)
    identity = json.dumps(
        [contract.child_request_digest(request), canonical_attempt],
        ensure_ascii=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return "spawn-" + hashlib.sha256(identity).hexdigest()


def build_spawn_transaction(
    request: dict[str, Any],
    *,
    attempt: int,
    created_at: str,
) -> dict[str, Any]:
    contract.validate_child_request(request)
    canonical_attempt = _validate_attempt(attempt)
    _parse_timestamp(created_at, field="spawn created_at")
    transaction = {
        "schema_version": SPAWN_TRANSACTION_SCHEMA_VERSION,
        "id": spawn_transaction_id(request, canonical_attempt),
        "child_request_id": request["id"],
        "child_request_digest": contract.child_request_digest(request),
        "bootstrap_digest": bootstrap.child_bootstrap_digest(request),
        "attempt": canonical_attempt,
        "state": "pending",
        "tab_id": None,
        "child_conversation_url": None,
        "failure_reason": None,
        "created_at": created_at,
        "updated_at": created_at,
    }
    validate_spawn_transaction(transaction, request=request)
    return transaction


def validate_spawn_transaction(
    transaction: dict[str, Any],
    *,
    request: dict[str, Any],
) -> None:
    if not isinstance(transaction, dict):
        raise ValueError("spawn transaction must be an object")
    contract.validate_child_request(request)
    if len(_canonical_bytes(transaction)) > MAX_SPAWN_TRANSACTION_BYTES:
        raise ValueError(f"spawn transaction exceeds {MAX_SPAWN_TRANSACTION_BYTES} bytes")
    extra = set(transaction) - _TRANSACTION_FIELDS
    if extra:
        raise ValueError(f"spawn transaction contains unsupported fields: {sorted(extra)!r}")
    missing = _TRANSACTION_FIELDS - set(transaction)
    if missing:
        raise ValueError(f"spawn transaction is missing required fields: {sorted(missing)!r}")
    if (
        type(transaction.get("schema_version")) is not int
        or transaction["schema_version"] != SPAWN_TRANSACTION_SCHEMA_VERSION
    ):
        raise ValueError(
            "spawn transaction schema_version must be "
            f"{SPAWN_TRANSACTION_SCHEMA_VERSION}"
        )

    attempt = _validate_attempt(transaction.get("attempt"))
    expected_id = spawn_transaction_id(request, attempt)
    if transaction.get("id") != expected_id:
        raise ValueError("spawn transaction id does not match request and attempt")
    if transaction.get("child_request_id") != request["id"]:
        raise ValueError("spawn transaction child_request_id does not match request")
    if transaction.get("child_request_digest") != contract.child_request_digest(request):
        raise ValueError("spawn transaction child_request_digest does not match request")
    bootstrap.require_child_bootstrap_digest(
        request,
        expected_digest=transaction.get("bootstrap_digest"),
    )

    state = transaction.get("state")
    if not isinstance(state, str) or state not in SPAWN_STATES:
        raise ValueError(f"unsupported spawn transaction state: {state!r}")
    tab_id = transaction.get("tab_id")
    if tab_id is not None and (type(tab_id) is not int or tab_id < 1):
        raise ValueError("spawn tab_id must be a positive integer or null")

    child_url = transaction.get("child_conversation_url")
    requires_identity = state in {
        "identity_discovered",
        "registration_submitting",
        "done",
    }
    if child_url is None:
        if requires_identity:
            raise ValueError(f"spawn state {state!r} requires child conversation identity")
    else:
        canonical = contract.canonical_conversation_url(child_url)
        if child_url != canonical:
            raise ValueError("spawn child_conversation_url must use canonical ChatGPT form")
        if state in SAFE_PRE_SUBMIT_STATES or state in {"failed", "cancelled"}:
            raise ValueError(f"spawn state {state!r} cannot have child conversation identity")
        if child_url == request["parent_conversation_url"]:
            raise ValueError("spawn child conversation must differ from parent")

    exceptional = state in {"failed", "ambiguous", "cancelled"}
    _validate_reason(transaction.get("failure_reason"), required=exceptional)
    if not exceptional and transaction.get("failure_reason") is not None:
        raise ValueError(f"spawn state {state!r} cannot carry failure_reason")

    created = _parse_timestamp(transaction.get("created_at"), field="spawn created_at")
    updated = _parse_timestamp(transaction.get("updated_at"), field="spawn updated_at")
    if updated < created:
        raise ValueError("spawn updated_at cannot precede created_at")


def transition_spawn_transaction(
    transaction: dict[str, Any],
    *,
    request: dict[str, Any],
    target: str,
    updated_at: str,
    tab_id: int | None = None,
    child_conversation_url: str | None = None,
    reason: str | None = None,
) -> dict[str, Any]:
    validate_spawn_transaction(transaction, request=request)
    if not isinstance(target, str) or target not in SPAWN_STATES:
        raise ValueError(f"unsupported spawn transaction target: {target!r}")
    current = str(transaction["state"])
    if target == current:
        return dict(transaction)
    if target not in _ALLOWED_TRANSITIONS[current]:
        raise ValueError(f"invalid spawn transaction transition: {current} -> {target}")
    _parse_timestamp(updated_at, field="spawn updated_at")

    updated = dict(transaction)
    if target == "tab_created":
        if type(tab_id) is not int or tab_id < 1:
            raise ValueError("tab_created transition requires a positive tab_id")
        updated["tab_id"] = tab_id
    elif tab_id is not None:
        if type(tab_id) is not int or tab_id < 1:
            raise ValueError("spawn tab_id must be a positive integer")
        updated["tab_id"] = tab_id

    if target == "identity_discovered":
        if child_conversation_url is None:
            raise ValueError("identity_discovered transition requires child_conversation_url")
        updated["child_conversation_url"] = contract.canonical_conversation_url(
            child_conversation_url
        )
    elif child_conversation_url is not None:
        raise ValueError(
            "child_conversation_url may only be supplied while discovering identity"
        )

    if target in {"failed", "ambiguous", "cancelled"}:
        updated["failure_reason"] = _validate_reason(reason, required=True)
    elif reason is not None:
        raise ValueError("failure reason is only valid for exceptional terminal states")

    updated["state"] = target
    updated["updated_at"] = updated_at
    validate_spawn_transaction(updated, request=request)
    return updated


def fail_spawn_transaction(
    transaction: dict[str, Any],
    *,
    request: dict[str, Any],
    reason: str,
    updated_at: str,
) -> dict[str, Any]:
    validate_spawn_transaction(transaction, request=request)
    current = str(transaction["state"])
    if current in TERMINAL_SPAWN_STATES:
        raise ValueError("terminal spawn transaction cannot fail again")
    target = "failed" if current in SAFE_PRE_SUBMIT_STATES else "ambiguous"
    return transition_spawn_transaction(
        transaction,
        request=request,
        target=target,
        updated_at=updated_at,
        reason=reason,
    )


def cancel_spawn_transaction(
    transaction: dict[str, Any],
    *,
    request: dict[str, Any],
    reason: str,
    updated_at: str,
) -> dict[str, Any]:
    validate_spawn_transaction(transaction, request=request)
    current = str(transaction["state"])
    if current in TERMINAL_SPAWN_STATES:
        raise ValueError("terminal spawn transaction cannot be cancelled")
    target = "cancelled" if current in SAFE_PRE_SUBMIT_STATES else "ambiguous"
    return transition_spawn_transaction(
        transaction,
        request=request,
        target=target,
        updated_at=updated_at,
        reason=reason,
    )


def clear_spawn_tab_cache(
    transaction: dict[str, Any],
    *,
    request: dict[str, Any],
    updated_at: str,
) -> dict[str, Any]:
    validate_spawn_transaction(transaction, request=request)
    _parse_timestamp(updated_at, field="spawn updated_at")
    updated = dict(transaction)
    updated["tab_id"] = None
    updated["updated_at"] = updated_at
    validate_spawn_transaction(updated, request=request)
    return updated
