from __future__ import annotations

import math
import re
from dataclasses import dataclass
from enum import Enum
from typing import Any
from urllib.parse import urlsplit

from local_agent.mcp.errors import MCPConfigError

_SERVER_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_MIME_RE = re.compile(r"^[a-z0-9][a-z0-9!#$&^_.+-]*/[a-z0-9][a-z0-9!#$&^_.+-]*$")
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})
_ALLOWED_TRANSPORT = "streamable_http"
MAX_SERVER_RECORDS = 128
MAX_TOOL_POLICIES = 512
MAX_MIME_TYPES = 64
MAX_TIMEOUT_SECONDS = 300.0
MAX_ARGUMENT_BYTES = 64 * 1024
MAX_TEXT_BYTES = 1024 * 1024
MAX_ARTIFACT_BYTES = 64 * 1024 * 1024
MAX_TOOLS = 512
MAX_ARTIFACTS = 32


class RiskClass(str, Enum):
    READ = "read"
    WRITE = "write"
    ARBITRARY_CODE = "arbitrary_code"


@dataclass(frozen=True)
class ToolPolicy:
    name: str
    risk: RiskClass
    enabled: bool


@dataclass(frozen=True)
class MCPServerConfig:
    server_id: str
    transport: str
    endpoint: str
    enabled: bool
    tools: tuple[ToolPolicy, ...]
    connect_timeout_seconds: float
    call_timeout_seconds: float
    max_text_bytes: int
    max_artifact_bytes: int
    max_tools: int
    max_artifacts: int
    allowed_artifact_mime_types: tuple[str, ...]

    def tool_policy(self, name: str) -> ToolPolicy | None:
        return next((policy for policy in self.tools if policy.name == name), None)


def _require_object(value: Any, field: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise MCPConfigError(f"{field} must be an object")
    return value


def _require_bool(value: Any, field: str) -> bool:
    if not isinstance(value, bool):
        raise MCPConfigError(f"{field} must be a boolean")
    return value


def _bounded_number(value: Any, field: str, *, minimum: float, maximum: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise MCPConfigError(f"{field} must be a number")
    result = float(value)
    if not math.isfinite(result) or result < minimum or result > maximum:
        raise MCPConfigError(f"{field} must be finite and between {minimum} and {maximum}")
    return result


def _bounded_int(value: Any, field: str, *, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise MCPConfigError(f"{field} must be an integer")
    if value < minimum or value > maximum:
        raise MCPConfigError(f"{field} must be between {minimum} and {maximum}")
    return value


def validate_loopback_endpoint(endpoint: Any) -> str:
    if not isinstance(endpoint, str) or not endpoint or endpoint != endpoint.strip():
        raise MCPConfigError("endpoint must be non-empty canonical text")
    try:
        parsed = urlsplit(endpoint)
        port = parsed.port
    except ValueError as exc:
        raise MCPConfigError(f"invalid endpoint: {exc}") from None
    if parsed.scheme not in {"http", "https"}:
        raise MCPConfigError("endpoint scheme must be http or https")
    if parsed.username is not None or parsed.password is not None:
        raise MCPConfigError("endpoint must not contain user information")
    if parsed.fragment:
        raise MCPConfigError("endpoint must not contain a fragment")
    if parsed.hostname is None or parsed.hostname.casefold() not in _LOOPBACK_HOSTS:
        raise MCPConfigError("endpoint host must be 127.0.0.1, ::1, or localhost")
    if port is not None and not 1 <= port <= 65535:
        raise MCPConfigError("endpoint port is out of range")
    return endpoint


def _parse_tool_policy(value: Any, *, server_id: str) -> ToolPolicy:
    record = _require_object(value, f"server {server_id!r} tool policy")
    allowed = {"name", "risk", "enabled"}
    unexpected = set(record) - allowed
    if unexpected:
        raise MCPConfigError(
            f"server {server_id!r} tool policy has unsupported fields: {sorted(unexpected)!r}"
        )
    name = record.get("name")
    if not isinstance(name, str) or not name or len(name) > 256 or name != name.strip():
        raise MCPConfigError(f"server {server_id!r} tool name must be bounded canonical text")
    try:
        risk = RiskClass(record.get("risk"))
    except (TypeError, ValueError):
        raise MCPConfigError(
            f"server {server_id!r} tool {name!r} risk must be read, write, or arbitrary_code"
        ) from None
    enabled = _require_bool(record.get("enabled", True), f"tool {name!r} enabled")
    return ToolPolicy(name=name, risk=risk, enabled=enabled)


def parse_server_config(value: Any) -> MCPServerConfig:
    record = _require_object(value, "server")
    allowed = {
        "id",
        "transport",
        "endpoint",
        "enabled",
        "tools",
        "connect_timeout_seconds",
        "call_timeout_seconds",
        "max_text_bytes",
        "max_artifact_bytes",
        "max_tools",
        "max_artifacts",
        "allowed_artifact_mime_types",
    }
    unexpected = set(record) - allowed
    if unexpected:
        raise MCPConfigError(f"server has unsupported fields: {sorted(unexpected)!r}")

    server_id = record.get("id")
    if not isinstance(server_id, str) or not _SERVER_ID_RE.fullmatch(server_id):
        raise MCPConfigError("server id must be lowercase [a-z0-9._-] text up to 64 characters")
    transport = record.get("transport")
    if transport != _ALLOWED_TRANSPORT:
        raise MCPConfigError(f"server {server_id!r} transport must be {_ALLOWED_TRANSPORT!r}")
    endpoint = validate_loopback_endpoint(record.get("endpoint"))
    enabled = _require_bool(record.get("enabled", True), f"server {server_id!r} enabled")

    raw_tools = record.get("tools", [])
    if not isinstance(raw_tools, list):
        raise MCPConfigError(f"server {server_id!r} tools must be a list")
    if len(raw_tools) > MAX_TOOL_POLICIES:
        raise MCPConfigError(f"server {server_id!r} tools exceeds {MAX_TOOL_POLICIES} items")
    tools = tuple(_parse_tool_policy(item, server_id=server_id) for item in raw_tools)
    names = [policy.name for policy in tools]
    if len(names) != len(set(names)):
        raise MCPConfigError(f"server {server_id!r} contains duplicate tool policies")

    raw_mimes = record.get("allowed_artifact_mime_types", [])
    if not isinstance(raw_mimes, list) or len(raw_mimes) > MAX_MIME_TYPES:
        raise MCPConfigError(
            f"server {server_id!r} allowed_artifact_mime_types must be a bounded list"
        )
    mimes: list[str] = []
    for mime in raw_mimes:
        if not isinstance(mime, str) or mime != mime.strip() or mime != mime.casefold():
            raise MCPConfigError(f"server {server_id!r} artifact MIME types must be canonical")
        if not _MIME_RE.fullmatch(mime):
            raise MCPConfigError(f"server {server_id!r} has invalid artifact MIME type {mime!r}")
        if mime in mimes:
            raise MCPConfigError(f"server {server_id!r} has duplicate artifact MIME type {mime!r}")
        mimes.append(mime)

    return MCPServerConfig(
        server_id=server_id,
        transport=transport,
        endpoint=endpoint,
        enabled=enabled,
        tools=tools,
        connect_timeout_seconds=_bounded_number(
            record.get("connect_timeout_seconds", 10.0),
            f"server {server_id!r} connect_timeout_seconds",
            minimum=0.1,
            maximum=MAX_TIMEOUT_SECONDS,
        ),
        call_timeout_seconds=_bounded_number(
            record.get("call_timeout_seconds", 30.0),
            f"server {server_id!r} call_timeout_seconds",
            minimum=0.1,
            maximum=MAX_TIMEOUT_SECONDS,
        ),
        max_text_bytes=_bounded_int(
            record.get("max_text_bytes", 64 * 1024),
            f"server {server_id!r} max_text_bytes",
            minimum=1,
            maximum=MAX_TEXT_BYTES,
        ),
        max_artifact_bytes=_bounded_int(
            record.get("max_artifact_bytes", 8 * 1024 * 1024),
            f"server {server_id!r} max_artifact_bytes",
            minimum=1,
            maximum=MAX_ARTIFACT_BYTES,
        ),
        max_tools=_bounded_int(
            record.get("max_tools", 128),
            f"server {server_id!r} max_tools",
            minimum=1,
            maximum=MAX_TOOLS,
        ),
        max_artifacts=_bounded_int(
            record.get("max_artifacts", 8),
            f"server {server_id!r} max_artifacts",
            minimum=1,
            maximum=MAX_ARTIFACTS,
        ),
        allowed_artifact_mime_types=tuple(mimes),
    )
