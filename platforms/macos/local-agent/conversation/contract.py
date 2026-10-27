from __future__ import annotations

import hashlib
import json
import re
import secrets
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse

from local_agent.repository.binding import canonical_agent_binding

CHILD_REQUEST_SCHEMA_VERSION = 2
CHILD_REGISTRATION_SCHEMA_VERSION = 2
BOOTSTRAP_CONTRACT_VERSION = 1
MAX_CHILD_REQUEST_BYTES = 32 * 1024
MAX_CHILD_REGISTRATION_BYTES = 8 * 1024
MAX_ID_CHARS = 200
MAX_REPOSITORY_ID_CHARS = 120
MAX_REPOSITORY_REF_CHARS = 300
MAX_SCOPE_SUMMARY_CHARS = 4096
MAX_SCOPE_PATHS = 64
MAX_SCOPE_PATH_CHARS = 1024
MAX_CONTEXT_REFS = 64
MAX_CONTEXT_KIND_CHARS = 64
MAX_CONVERSATION_URL_CHARS = 512
MAX_WORKFLOW_NODE_REVISION = 2**31 - 1

CHILD_ROLES = frozenset({"research", "implementation", "verification", "integration"})

_ID_RE = re.compile(r"^[A-Za-z0-9._-]+$")
_REPOSITORY_ID_RE = re.compile(r"^[A-Za-z0-9._-]+$")
_CONVERSATION_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,200}$")
_CONTEXT_KIND_RE = re.compile(r"^[a-z][a-z0-9._-]*$")
_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_COMMIT_SHA_RE = re.compile(r"^[0-9a-f]{40}$")


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
        raise ValueError("conversation record must be canonical JSON data") from exc
    return text.encode("utf-8")


def _validate_record_size(payload: Any, *, maximum: int, field: str) -> None:
    if len(_canonical_bytes(payload)) > maximum:
        raise ValueError(f"{field} exceeds {maximum} bytes")


def _validate_exact_fields(
    payload: dict[str, Any],
    *,
    allowed: frozenset[str],
    required: frozenset[str],
    field: str,
) -> None:
    extra = set(payload) - allowed
    if extra:
        raise ValueError(f"{field} contains unsupported fields: {sorted(extra)!r}")
    missing = required - set(payload)
    if missing:
        raise ValueError(f"{field} is missing required fields: {sorted(missing)!r}")


def _validate_id(value: Any, *, field: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > MAX_ID_CHARS
        or value != value.strip()
        or not _ID_RE.fullmatch(value)
    ):
        raise ValueError(
            f"{field} must be a canonical non-empty identifier up to {MAX_ID_CHARS} characters"
        )
    return value


def _validate_repository_id(value: Any) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > MAX_REPOSITORY_ID_CHARS
        or value != value.strip()
        or not _REPOSITORY_ID_RE.fullmatch(value)
    ):
        raise ValueError("repository_id must be a bounded canonical repository id")
    return value


def _validate_digest(value: Any, *, field: str) -> str:
    if not isinstance(value, str) or not _DIGEST_RE.fullmatch(value):
        raise ValueError(f"{field} must be a canonical sha256 digest")
    return value


def _validate_workflow_node_revision(value: Any) -> int:
    if type(value) is not int or value < 0 or value > MAX_WORKFLOW_NODE_REVISION:
        raise ValueError(
            f"workflow_node_revision must be 0..{MAX_WORKFLOW_NODE_REVISION}"
        )
    return value


def _parse_utc_timestamp(value: Any, *, field: str) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z") or len(value) > 64:
        raise ValueError(f"{field} must be a bounded RFC3339 UTC timestamp ending in Z")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise ValueError(f"{field} must be a valid RFC3339 UTC timestamp") from exc
    if parsed.tzinfo != timezone.utc:
        raise ValueError(f"{field} must use UTC")
    return parsed


def canonical_conversation_url(value: Any) -> str:
    if not isinstance(value, str) or not value or len(value) > MAX_CONVERSATION_URL_CHARS:
        raise ValueError("conversation URL must be a non-empty bounded string")
    if value != value.strip():
        raise ValueError("conversation URL must not contain surrounding whitespace")
    try:
        parsed = urlparse(value)
        port = parsed.port
    except ValueError as exc:
        raise ValueError("conversation URL is invalid") from exc
    if parsed.scheme != "https":
        raise ValueError("conversation URL must use https")
    if parsed.hostname not in {"chatgpt.com", "chat.openai.com"}:
        raise ValueError("conversation URL must use the ChatGPT origin")
    if parsed.username is not None or parsed.password is not None or port is not None:
        raise ValueError("conversation URL must not contain credentials or a port")
    if parsed.query or parsed.fragment:
        raise ValueError("conversation URL must not contain query or fragment data")
    match = re.fullmatch(r"/c/([^/]+)/?", parsed.path)
    if match is None or not _CONVERSATION_ID_RE.fullmatch(match.group(1)):
        raise ValueError("conversation URL must identify one concrete ChatGPT conversation")
    return f"https://chatgpt.com/c/{match.group(1)}"


def _validate_canonical_conversation_url(value: Any, *, field: str) -> str:
    canonical = canonical_conversation_url(value)
    if value != canonical:
        raise ValueError(f"{field} must use canonical ChatGPT conversation URL form")
    return canonical


def _validate_repository_ref(value: Any) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > MAX_REPOSITORY_REF_CHARS
        or value != value.strip()
    ):
        raise ValueError("repository_ref must be a non-empty bounded string")
    if any(ord(char) < 32 or char.isspace() for char in value):
        raise ValueError("repository_ref must not contain whitespace or control characters")
    if value == "@" or value.startswith("/") or value.endswith("/") or "//" in value:
        raise ValueError("repository_ref is not canonical")
    if any(token in value for token in ("..", "@{", "\\", "~", "^", ":", "?", "*", "[")):
        raise ValueError("repository_ref contains unsupported Git ref characters")
    for component in value.split("/"):
        if (
            not component
            or component.startswith(".")
            or component.endswith(".")
            or component.endswith(".lock")
        ):
            raise ValueError("repository_ref contains an unsupported Git ref component")
    return value


def _validate_repository_commit_sha(value: Any) -> str:
    if not isinstance(value, str) or not _COMMIT_SHA_RE.fullmatch(value):
        raise ValueError("repository_commit_sha must be an exact lowercase 40-character commit SHA")
    return value


def _validate_scope_path(value: Any) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > MAX_SCOPE_PATH_CHARS
        or "\\" in value
        or value.startswith("/")
        or any(ord(char) < 32 or ord(char) == 127 for char in value)
    ):
        raise ValueError(
            "child request scope paths must be canonical repository-relative POSIX paths"
        )
    components = value.split("/")
    if any(component in {"", ".", ".."} for component in components):
        raise ValueError(
            "child request scope paths must be canonical repository-relative POSIX paths"
        )
    return value


def _validate_scope(scope: Any) -> None:
    if not isinstance(scope, dict):
        raise ValueError("child request scope must be an object")
    _validate_exact_fields(
        scope,
        allowed=frozenset({"summary", "paths"}),
        required=frozenset({"summary"}),
        field="child request scope",
    )
    summary = scope.get("summary")
    if (
        not isinstance(summary, str)
        or not summary
        or summary != summary.strip()
        or len(summary) > MAX_SCOPE_SUMMARY_CHARS
    ):
        raise ValueError("child request scope summary must be a non-empty bounded string")
    paths = scope.get("paths", [])
    if not isinstance(paths, list):
        raise ValueError("child request scope paths must be a list")
    if len(paths) > MAX_SCOPE_PATHS:
        raise ValueError(f"child request scope paths exceeds {MAX_SCOPE_PATHS} items")
    seen: set[str] = set()
    for raw_path in paths:
        path = _validate_scope_path(raw_path)
        if path in seen:
            raise ValueError(f"duplicate child request scope path: {path!r}")
        seen.add(path)


def _validate_context_refs(value: Any) -> None:
    if not isinstance(value, list):
        raise ValueError("child request context_refs must be a list")
    if len(value) > MAX_CONTEXT_REFS:
        raise ValueError(f"child request context_refs exceeds {MAX_CONTEXT_REFS} items")
    seen: set[tuple[str, str]] = set()
    for raw in value:
        if not isinstance(raw, dict):
            raise ValueError("child request context_refs items must be objects")
        fields = frozenset({"kind", "id", "digest"})
        _validate_exact_fields(
            raw,
            allowed=fields,
            required=fields,
            field="child request context reference",
        )
        kind = raw.get("kind")
        if (
            not isinstance(kind, str)
            or not kind
            or len(kind) > MAX_CONTEXT_KIND_CHARS
            or kind != kind.strip()
            or kind != kind.casefold()
            or not _CONTEXT_KIND_RE.fullmatch(kind)
        ):
            raise ValueError(
                "child request context reference kind must be a canonical lowercase identifier"
            )
        reference_id = _validate_id(raw.get("id"), field="child request context reference id")
        _validate_digest(raw.get("digest"), field="child request context reference digest")
        key = (kind, reference_id)
        if key in seen:
            raise ValueError(f"duplicate child request context reference: {kind}:{reference_id}")
        seen.add(key)


def validate_child_request(request: dict[str, Any]) -> None:
    if not isinstance(request, dict):
        raise ValueError("child request must be an object")
    _validate_record_size(request, maximum=MAX_CHILD_REQUEST_BYTES, field="child request")
    allowed = frozenset(
        {
            "schema_version",
            "id",
            "workflow_id",
            "workflow_node_id",
            "workflow_node_revision",
            "workflow_node_introduction_digest",
            "parent_conversation_url",
            "created_at",
            "role",
            "repository_id",
            "agent_binding",
            "repository_ref",
            "repository_commit_sha",
            "scope",
            "context_refs",
            "bootstrap_contract_version",
        }
    )
    _validate_exact_fields(request, allowed=allowed, required=allowed, field="child request")
    if (
        type(request.get("schema_version")) is not int
        or request["schema_version"] != CHILD_REQUEST_SCHEMA_VERSION
    ):
        raise ValueError(f"child request schema_version must be {CHILD_REQUEST_SCHEMA_VERSION}")
    _validate_id(request.get("id"), field="child request id")
    _validate_id(request.get("workflow_id"), field="workflow_id")
    _validate_id(request.get("workflow_node_id"), field="workflow_node_id")
    _validate_workflow_node_revision(request.get("workflow_node_revision"))
    _validate_digest(
        request.get("workflow_node_introduction_digest"),
        field="workflow_node_introduction_digest",
    )
    _validate_canonical_conversation_url(
        request.get("parent_conversation_url"), field="parent_conversation_url"
    )
    _parse_utc_timestamp(request.get("created_at"), field="child request created_at")
    role = request.get("role")
    if not isinstance(role, str) or role not in CHILD_ROLES:
        raise ValueError(f"child request role must be one of {sorted(CHILD_ROLES)!r}")
    _validate_repository_id(request.get("repository_id"))
    canonical_agent_binding(request.get("agent_binding"), field="child request agent_binding")
    _validate_repository_ref(request.get("repository_ref"))
    _validate_repository_commit_sha(request.get("repository_commit_sha"))
    _validate_scope(request.get("scope"))
    _validate_context_refs(request.get("context_refs"))
    if (
        type(request.get("bootstrap_contract_version")) is not int
        or request["bootstrap_contract_version"] != BOOTSTRAP_CONTRACT_VERSION
    ):
        raise ValueError(f"bootstrap_contract_version must be {BOOTSTRAP_CONTRACT_VERSION}")


def _child_request_identity_payload(request: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in request.items() if key != "created_at"}


def child_request_digest(request: dict[str, Any]) -> str:
    validate_child_request(request)
    return "sha256:" + hashlib.sha256(
        _canonical_bytes(_child_request_identity_payload(request))
    ).hexdigest()


def require_child_request_target(
    request: dict[str, Any], *, repository_id: str, agent_binding: str
) -> None:
    validate_child_request(request)
    expected_repository_id = _validate_repository_id(repository_id)
    expected_binding = canonical_agent_binding(agent_binding, field="expected_agent_binding")
    if request["repository_id"] != expected_repository_id:
        raise ValueError(
            "child request repository mismatch: "
            f"expected {expected_repository_id!r}, got {request['repository_id']!r}"
        )
    if not secrets.compare_digest(str(request["agent_binding"]), expected_binding):
        raise ValueError("child request agent_binding mismatch")


def require_child_request_workflow_provenance(
    request: dict[str, Any],
    *,
    workflow_id: str,
    workflow_node_id: str,
    workflow_node_revision: int,
    workflow_node_introduction_digest: str,
) -> None:
    validate_child_request(request)
    expected_workflow_id = _validate_id(workflow_id, field="expected workflow_id")
    expected_node_id = _validate_id(workflow_node_id, field="expected workflow_node_id")
    expected_revision = _validate_workflow_node_revision(workflow_node_revision)
    expected_digest = _validate_digest(
        workflow_node_introduction_digest,
        field="expected workflow_node_introduction_digest",
    )
    if request["workflow_id"] != expected_workflow_id:
        raise ValueError("child request workflow_id mismatch")
    if request["workflow_node_id"] != expected_node_id:
        raise ValueError("child request workflow_node_id mismatch")
    if request["workflow_node_revision"] != expected_revision:
        raise ValueError("child request workflow_node_revision mismatch")
    if not secrets.compare_digest(
        str(request["workflow_node_introduction_digest"]), expected_digest
    ):
        raise ValueError("child request workflow_node_introduction_digest mismatch")


def validate_child_registration(
    registration: dict[str, Any], *, request: dict[str, Any] | None = None
) -> None:
    if not isinstance(registration, dict):
        raise ValueError("child registration must be an object")
    _validate_record_size(
        registration, maximum=MAX_CHILD_REGISTRATION_BYTES, field="child registration"
    )
    allowed = frozenset(
        {
            "schema_version",
            "child_request_id",
            "child_request_digest",
            "parent_conversation_url",
            "child_conversation_url",
            "registered_at",
        }
    )
    _validate_exact_fields(
        registration,
        allowed=allowed,
        required=allowed,
        field="child registration",
    )
    if (
        type(registration.get("schema_version")) is not int
        or registration["schema_version"] != CHILD_REGISTRATION_SCHEMA_VERSION
    ):
        raise ValueError(
            f"child registration schema_version must be {CHILD_REGISTRATION_SCHEMA_VERSION}"
        )
    _validate_id(registration.get("child_request_id"), field="child_request_id")
    digest = registration.get("child_request_digest")
    if not isinstance(digest, str) or not _DIGEST_RE.fullmatch(digest):
        raise ValueError("child_request_digest must be a canonical sha256 digest")
    parent_url = _validate_canonical_conversation_url(
        registration.get("parent_conversation_url"), field="parent_conversation_url"
    )
    child_url = _validate_canonical_conversation_url(
        registration.get("child_conversation_url"), field="child_conversation_url"
    )
    if child_url == parent_url:
        raise ValueError("child conversation must differ from parent conversation")
    _parse_utc_timestamp(
        registration.get("registered_at"), field="child registration registered_at"
    )

    if request is None:
        return
    validate_child_request(request)
    expected_digest = child_request_digest(request)
    if registration["child_request_id"] != request["id"]:
        raise ValueError("child registration request id does not match admitted request")
    if not secrets.compare_digest(str(registration["child_request_digest"]), expected_digest):
        raise ValueError("child registration request digest does not match admitted request")
    if registration["parent_conversation_url"] != request["parent_conversation_url"]:
        raise ValueError("child registration parent does not match admitted request")


def reconcile_child_registration(
    existing: dict[str, Any] | None,
    candidate: dict[str, Any],
    *,
    request: dict[str, Any],
) -> dict[str, Any]:
    validate_child_registration(candidate, request=request)
    if existing is None:
        return dict(candidate)

    validate_child_registration(existing, request=request)
    for field, message in (
        ("child_request_id", "conflicting child registration request id"),
        ("child_request_digest", "conflicting child registration request digest"),
        ("parent_conversation_url", "conflicting child registration parent"),
        ("child_conversation_url", "conflicting child conversation URL"),
    ):
        if candidate[field] != existing[field]:
            raise ValueError(message)
    return dict(existing)
