from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
from typing import Any

from local_agent.mcp.config import MAX_ARGUMENT_BYTES
from local_agent.mcp.errors import MCPArgumentsError, MCPBoundaryError
from local_agent.mcp.registry import MCPServerRegistry, default_registry_path


def _print_json(payload: Any) -> None:
    print(json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True))


def _registry(args: argparse.Namespace) -> MCPServerRegistry:
    return MCPServerRegistry.load(Path(args.registry))


def _server_record(server: Any) -> dict[str, Any]:
    return {
        "id": server.server_id,
        "transport": server.transport,
        "endpoint": server.endpoint,
        "enabled": server.enabled,
        "connect_timeout_seconds": server.connect_timeout_seconds,
        "call_timeout_seconds": server.call_timeout_seconds,
        "max_text_bytes": server.max_text_bytes,
        "max_artifact_bytes": server.max_artifact_bytes,
        "max_tools": server.max_tools,
        "max_artifacts": server.max_artifacts,
        "allowed_artifact_mime_types": list(server.allowed_artifact_mime_types),
        "tools": [
            {
                "name": policy.name,
                "risk": policy.risk.value,
                "enabled": policy.enabled,
            }
            for policy in server.tools
        ],
    }


def command_servers(args: argparse.Namespace) -> int:
    registry = _registry(args)
    _print_json(
        {
            "ok": True,
            "registry": str(registry.source) if registry.source is not None else None,
            "servers": [_server_record(server) for server in registry.servers],
        }
    )
    return 0


def command_tools(args: argparse.Namespace) -> int:
    from local_agent.mcp.client import discover_tools

    result = asyncio.run(discover_tools(_registry(args), args.server_id))
    _print_json(result)
    return 0


def _arguments(raw: str) -> dict[str, Any]:
    if len(raw.encode("utf-8")) > MAX_ARGUMENT_BYTES:
        raise MCPArgumentsError(f"tool arguments exceed {MAX_ARGUMENT_BYTES} bytes")
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise MCPArgumentsError(f"tool arguments are not valid JSON: {exc}") from None
    if not isinstance(value, dict):
        raise MCPArgumentsError("tool arguments must be a JSON object")
    return value


def command_call(args: argparse.Namespace) -> int:
    from local_agent.mcp.client import call_tool

    result = asyncio.run(
        call_tool(
            _registry(args),
            args.server_id,
            args.tool_name,
            _arguments(args.arguments),
            intent=args.intent,
            artifact_dir=Path(args.artifact_dir).expanduser() if args.artifact_dir else None,
        )
    )
    _print_json(result)
    return 0 if result.get("ok") else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Local Agent local MCP client")
    parser.add_argument(
        "--registry",
        default=str(default_registry_path()),
        help="machine-local MCP server registry",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    servers = sub.add_parser("servers", help="show configured local MCP servers")
    servers.set_defaults(func=command_servers)

    tools = sub.add_parser("tools", help="discover tools from one configured MCP server")
    tools.add_argument("server_id")
    tools.set_defaults(func=command_tools)

    call = sub.add_parser("call", help="invoke one locally authorized MCP tool")
    call.add_argument("server_id")
    call.add_argument("tool_name")
    call.add_argument("--arguments", default="{}", help="bounded JSON object")
    call.add_argument(
        "--intent",
        choices=("read", "write", "arbitrary_code"),
        help="explicit invocation intent; required for write and arbitrary_code tools",
    )
    call.add_argument(
        "--artifact-dir",
        help="artifact directory; defaults to Local Agent machine state",
    )
    call.set_defaults(func=command_call)
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        return int(args.func(args))
    except MCPBoundaryError as exc:
        _print_json({"ok": False, "error": exc.as_dict()})
        return 2
    except ModuleNotFoundError as exc:
        if exc.name == "mcp":
            _print_json(
                {
                    "ok": False,
                    "error": {
                        "code": "dependency_missing",
                        "message": "official MCP Python SDK is not installed",
                    },
                }
            )
            return 2
        raise


if __name__ == "__main__":
    raise SystemExit(main())
