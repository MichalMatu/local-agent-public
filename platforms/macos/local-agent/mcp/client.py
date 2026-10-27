from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator

import httpx2
from mcp import Client
from mcp import types
from mcp.client.streamable_http import streamable_http_client

from local_agent.mcp.artifacts import PendingArtifact, commit_artifact, prepare_artifact
from local_agent.mcp.config import MAX_ARGUMENT_BYTES, MCPServerConfig
from local_agent.mcp.errors import (
    MCPArgumentsError,
    MCPArtifactError,
    MCPBoundaryError,
    MCPCallTimeoutError,
    MCPConnectionTimeoutError,
    MCPProtocolError,
    MCPResultTooLargeError,
    MCPTransportError,
    MCPUnknownToolError,
)
from local_agent.mcp.policy import authorize_tool
from local_agent.mcp.registry import MCPServerRegistry, default_artifact_dir


def _model_json(value: Any) -> Any:
    if value is None:
        return None
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json", by_alias=True, exclude_none=True)
    return value


def _bounded_payload(payload: dict[str, Any], max_bytes: int) -> dict[str, Any]:
    try:
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise MCPProtocolError(f"MCP result is not JSON-serializable: {exc}") from None
    if len(encoded) > max_bytes:
        raise MCPResultTooLargeError(
            f"MCP textual result is {len(encoded)} bytes; configured bound is {max_bytes} bytes"
        )
    return payload


def _bounded_arguments(arguments: Any) -> dict[str, Any]:
    if not isinstance(arguments, dict):
        raise MCPArgumentsError("tool arguments must be a JSON object")
    try:
        encoded = json.dumps(
            arguments,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise MCPArgumentsError(f"tool arguments are not JSON-serializable: {exc}") from None
    if len(encoded) > MAX_ARGUMENT_BYTES:
        raise MCPArgumentsError(f"tool arguments exceed {MAX_ARGUMENT_BYTES} bytes")
    return arguments


def _nested_exceptions(exc: BaseException) -> tuple[BaseException, ...]:
    children = getattr(exc, "exceptions", ())
    if not isinstance(children, (tuple, list)):
        return ()
    return tuple(child for child in children if isinstance(child, BaseException))


def _find_boundary_error(exc: BaseException) -> MCPBoundaryError | None:
    if isinstance(exc, MCPBoundaryError):
        return exc
    for child in _nested_exceptions(exc):
        found = _find_boundary_error(child)
        if found is not None:
            return found
    return None


def _contains_timeout(exc: BaseException) -> bool:
    if isinstance(exc, TimeoutError) or "Timeout" in type(exc).__name__:
        return True
    return any(_contains_timeout(child) for child in _nested_exceptions(exc))


@asynccontextmanager
async def _connected(server: MCPServerConfig) -> AsyncIterator[Client]:
    entered = False
    try:
        async with httpx2.AsyncClient(
            timeout=httpx2.Timeout(server.connect_timeout_seconds),
            trust_env=False,
        ) as http_client:
            transport = streamable_http_client(server.endpoint, http_client=http_client)
            async with Client(transport) as client:
                entered = True
                # Local Agent owns call timeout semantics. Keep the HTTP read timeout
                # slightly looser so asyncio.timeout below wins deterministically.
                http_client.timeout = httpx2.Timeout(
                    server.call_timeout_seconds + 1.0,
                    connect=server.connect_timeout_seconds,
                )
                yield client
    except MCPBoundaryError:
        raise
    except Exception as exc:
        boundary_error = _find_boundary_error(exc)
        if boundary_error is not None:
            raise boundary_error
        if not entered and _contains_timeout(exc):
            raise MCPConnectionTimeoutError(
                f"MCP connection to {server.server_id!r} exceeded "
                f"{server.connect_timeout_seconds:g}s"
            ) from None
        raise MCPTransportError(
            f"MCP transport for {server.server_id!r} failed: {type(exc).__name__}: {exc}"
        ) from None


async def _list_all_tools(client: Client, server: MCPServerConfig) -> list[Any]:
    tools: list[Any] = []
    cursor: str | None = None
    seen_cursors: set[str] = set()
    try:
        async with asyncio.timeout(server.call_timeout_seconds):
            while True:
                page = await client.list_tools(cursor=cursor)
                page_tools = getattr(page, "tools", None)
                if not isinstance(page_tools, list):
                    raise MCPProtocolError("MCP tools/list response has no tools list")
                tools.extend(page_tools)
                if len(tools) > server.max_tools:
                    raise MCPResultTooLargeError(
                        f"MCP server exposed more than configured {server.max_tools} tools"
                    )
                next_cursor = getattr(page, "next_cursor", None)
                if next_cursor is None:
                    break
                if not isinstance(next_cursor, str) or not next_cursor:
                    raise MCPProtocolError("MCP tools/list returned an invalid pagination cursor")
                if next_cursor in seen_cursors:
                    raise MCPProtocolError("MCP tools/list repeated a pagination cursor")
                seen_cursors.add(next_cursor)
                cursor = next_cursor
    except TimeoutError:
        raise MCPCallTimeoutError(
            f"MCP tools/list for {server.server_id!r} exceeded {server.call_timeout_seconds:g}s"
        ) from None
    except MCPBoundaryError:
        raise
    except Exception as exc:
        raise MCPProtocolError(
            f"MCP tools/list for {server.server_id!r} failed: {type(exc).__name__}: {exc}"
        ) from None

    names: list[str] = []
    for tool in tools:
        name = getattr(tool, "name", None)
        if not isinstance(name, str) or not name:
            raise MCPProtocolError("MCP tools/list returned a tool without a valid name")
        names.append(name)
    if len(names) != len(set(names)):
        raise MCPProtocolError("MCP tools/list returned duplicate tool names")
    return tools


def _connection_facts(client: Client) -> dict[str, Any]:
    return {
        "protocol_version": str(client.protocol_version),
        "server_info": _model_json(client.server_info),
        "server_capabilities": _model_json(client.server_capabilities),
    }


def _tool_record(tool: Any, server: MCPServerConfig) -> dict[str, Any]:
    raw = _model_json(tool)
    if not isinstance(raw, dict):
        raise MCPProtocolError("MCP tool metadata is not an object")
    name = getattr(tool, "name", None)
    assert isinstance(name, str)
    policy = server.tool_policy(name)
    raw["local_policy"] = (
        {"configured": False, "enabled": False, "risk": None}
        if policy is None
        else {
            "configured": True,
            "enabled": policy.enabled,
            "risk": policy.risk.value,
        }
    )
    return raw


async def discover_tools(
    registry: MCPServerRegistry,
    server_id: str,
) -> dict[str, Any]:
    server = registry.get(server_id)
    async with _connected(server) as client:
        tools = await _list_all_tools(client, server)
        payload = {
            "ok": True,
            "server_id": server.server_id,
            "transport": server.transport,
            **_connection_facts(client),
            "tools": [_tool_record(tool, server) for tool in tools],
        }
        return _bounded_payload(payload, server.max_text_bytes)


def _artifact_content(
    *,
    block_type: str,
    mime_type: str | None,
    encoded_data: str,
    server: MCPServerConfig,
    artifact_dir: Path,
    tool_name: str,
    index: int,
    uri: str | None = None,
) -> tuple[dict[str, Any], PendingArtifact]:
    pending = prepare_artifact(
        server=server,
        artifact_dir=artifact_dir,
        tool_name=tool_name,
        index=index,
        mime_type=mime_type,
        encoded_data=encoded_data,
    )
    content: dict[str, Any] = {
        "type": block_type,
        "artifact": pending.metadata(),
    }
    if uri is not None:
        content["uri"] = uri
    return content, pending


def _process_content(
    *,
    blocks: list[Any],
    server: MCPServerConfig,
    artifact_dir: Path,
    tool_name: str,
) -> tuple[list[dict[str, Any]], list[PendingArtifact]]:
    content: list[dict[str, Any]] = []
    pending: list[PendingArtifact] = []
    aggregate_binary_bytes = 0

    for block in blocks:
        if isinstance(block, types.TextContent):
            content.append({"type": "text", "text": block.text})
            continue
        if isinstance(block, types.ImageContent):
            item, artifact = _artifact_content(
                block_type="image",
                mime_type=block.mime_type,
                encoded_data=block.data,
                server=server,
                artifact_dir=artifact_dir,
                tool_name=tool_name,
                index=len(pending),
            )
        elif isinstance(block, types.AudioContent):
            item, artifact = _artifact_content(
                block_type="audio",
                mime_type=block.mime_type,
                encoded_data=block.data,
                server=server,
                artifact_dir=artifact_dir,
                tool_name=tool_name,
                index=len(pending),
            )
        elif isinstance(block, types.EmbeddedResource):
            resource = block.resource
            if isinstance(resource, types.TextResourceContents):
                content.append(
                    {
                        "type": "resource_text",
                        "uri": str(resource.uri),
                        "mime_type": resource.mime_type,
                        "text": resource.text,
                    }
                )
                continue
            if isinstance(resource, types.BlobResourceContents):
                item, artifact = _artifact_content(
                    block_type="resource_blob",
                    mime_type=resource.mime_type,
                    encoded_data=resource.blob,
                    server=server,
                    artifact_dir=artifact_dir,
                    tool_name=tool_name,
                    index=len(pending),
                    uri=str(resource.uri),
                )
            else:
                raise MCPProtocolError(
                    f"unsupported embedded MCP resource type: {type(resource).__name__}"
                )
        elif isinstance(block, types.ResourceLink):
            value = _model_json(block)
            if not isinstance(value, dict):
                raise MCPProtocolError("MCP resource link metadata is not an object")
            content.append(value)
            continue
        else:
            raise MCPProtocolError(f"unsupported MCP content block: {type(block).__name__}")

        aggregate_binary_bytes += artifact.size
        if len(pending) + 1 > server.max_artifacts:
            raise MCPResultTooLargeError(
                f"MCP result exceeds configured {server.max_artifacts} artifact limit"
            )
        if aggregate_binary_bytes > server.max_artifact_bytes:
            raise MCPResultTooLargeError(
                f"MCP result artifacts exceed configured {server.max_artifact_bytes}-byte aggregate bound"
            )
        content.append(item)
        pending.append(artifact)

    return content, pending


async def call_tool(
    registry: MCPServerRegistry,
    server_id: str,
    tool_name: str,
    arguments: dict[str, Any],
    *,
    intent: str | None = None,
    artifact_dir: Path | None = None,
) -> dict[str, Any]:
    server = registry.get(server_id)
    policy = authorize_tool(server, tool_name, intent=intent)
    bounded_arguments = _bounded_arguments(arguments)
    output_dir = default_artifact_dir() if artifact_dir is None else artifact_dir

    async with _connected(server) as client:
        tools = await _list_all_tools(client, server)
        discovery = {
            "ok": True,
            "server_id": server.server_id,
            "transport": server.transport,
            **_connection_facts(client),
            "tools": [_tool_record(tool, server) for tool in tools],
        }
        _bounded_payload(discovery, server.max_text_bytes)
        exposed = {getattr(tool, "name") for tool in tools}
        if tool_name not in exposed:
            raise MCPUnknownToolError(
                f"configured tool {tool_name!r} is not exposed by MCP server {server.server_id!r}"
            )

        try:
            async with asyncio.timeout(server.call_timeout_seconds):
                result = await client.call_tool(tool_name, bounded_arguments)
        except TimeoutError:
            raise MCPCallTimeoutError(
                f"MCP tool {tool_name!r} exceeded {server.call_timeout_seconds:g}s"
            ) from None
        except Exception as exc:
            raise MCPProtocolError(
                f"MCP tool {tool_name!r} failed at protocol boundary: "
                f"{type(exc).__name__}: {exc}"
            ) from None

        blocks = getattr(result, "content", None)
        if not isinstance(blocks, list):
            raise MCPProtocolError("MCP tool result has no content list")
        content, pending = _process_content(
            blocks=blocks,
            server=server,
            artifact_dir=output_dir,
            tool_name=tool_name,
        )
        payload = {
            "ok": not bool(getattr(result, "is_error", False)),
            "server_id": server.server_id,
            "tool": tool_name,
            "risk": policy.risk.value,
            "intent": intent,
            **_connection_facts(client),
            "is_error": bool(getattr(result, "is_error", False)),
            "content": content,
            "structured_content": getattr(result, "structured_content", None),
        }
        result_type = getattr(result, "result_type", None)
        if result_type is not None:
            payload["result_type"] = str(result_type)
        _bounded_payload(payload, server.max_text_bytes)

        for artifact in pending:
            try:
                commit_artifact(artifact)
            except OSError as exc:
                raise MCPArtifactError(
                    f"failed to persist MCP artifact {artifact.path}: {exc}"
                ) from exc
        return payload
