from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from local_agent.mcp.config import MAX_SERVER_RECORDS, MCPServerConfig, parse_server_config
from local_agent.mcp.errors import (
    MCPConfigError,
    MCPServerDisabledError,
    MCPUnknownServerError,
)

MAX_REGISTRY_BYTES = 1024 * 1024


def default_state_dir() -> Path:
    return Path.home() / "Library" / "Application Support" / "local-agent"


def default_registry_path() -> Path:
    return default_state_dir() / "mcp" / "servers.json"


def default_artifact_dir() -> Path:
    return default_state_dir() / "mcp" / "artifacts"


@dataclass(frozen=True)
class MCPServerRegistry:
    servers: tuple[MCPServerConfig, ...]
    source: Path | None = None

    @classmethod
    def load(cls, path: Path | None = None) -> MCPServerRegistry:
        source = default_registry_path() if path is None else path.expanduser()
        try:
            size = source.stat().st_size
        except FileNotFoundError:
            raise MCPConfigError(f"MCP registry not found: {source}") from None
        if size > MAX_REGISTRY_BYTES:
            raise MCPConfigError(f"MCP registry exceeds {MAX_REGISTRY_BYTES} bytes")
        try:
            raw = source.read_text(encoding="utf-8")
        except OSError as exc:
            raise MCPConfigError(f"cannot read MCP registry {source}: {exc}") from exc
        if len(raw.encode("utf-8")) > MAX_REGISTRY_BYTES:
            raise MCPConfigError(f"MCP registry exceeds {MAX_REGISTRY_BYTES} bytes")
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise MCPConfigError(f"invalid MCP registry JSON: {exc}") from None
        return cls.from_payload(payload, source=source)

    @classmethod
    def from_payload(
        cls,
        payload: Any,
        *,
        source: Path | None = None,
    ) -> MCPServerRegistry:
        if not isinstance(payload, dict):
            raise MCPConfigError("MCP registry must be an object")
        unexpected = set(payload) - {"version", "servers"}
        if unexpected:
            raise MCPConfigError(f"MCP registry has unsupported fields: {sorted(unexpected)!r}")
        if payload.get("version") != 1:
            raise MCPConfigError("MCP registry version must be 1")
        raw_servers = payload.get("servers")
        if not isinstance(raw_servers, list):
            raise MCPConfigError("MCP registry servers must be a list")
        if len(raw_servers) > MAX_SERVER_RECORDS:
            raise MCPConfigError(f"MCP registry exceeds {MAX_SERVER_RECORDS} servers")
        servers = tuple(parse_server_config(item) for item in raw_servers)
        ids = [server.server_id for server in servers]
        if len(ids) != len(set(ids)):
            raise MCPConfigError("MCP registry contains duplicate server ids")
        return cls(servers=servers, source=source)

    def get(self, server_id: str, *, require_enabled: bool = True) -> MCPServerConfig:
        server = next((item for item in self.servers if item.server_id == server_id), None)
        if server is None:
            raise MCPUnknownServerError(f"unknown MCP server: {server_id}")
        if require_enabled and not server.enabled:
            raise MCPServerDisabledError(f"MCP server is disabled: {server_id}")
        return server
