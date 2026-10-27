from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from local_agent.mcp.config import RiskClass, validate_loopback_endpoint
from local_agent.mcp.errors import (
    MCPConfigError,
    MCPIntentRequiredError,
    MCPServerDisabledError,
    MCPToolDeniedError,
    MCPUnknownServerError,
    MCPUnknownToolError,
)
from local_agent.mcp.policy import authorize_tool
from local_agent.mcp.registry import MCPServerRegistry


def server_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "id": "test-local",
        "transport": "streamable_http",
        "endpoint": "http://127.0.0.1:8765/mcp",
        "enabled": True,
        "connect_timeout_seconds": 1,
        "call_timeout_seconds": 2,
        "max_text_bytes": 4096,
        "max_artifact_bytes": 8192,
        "max_tools": 32,
        "max_artifacts": 4,
        "allowed_artifact_mime_types": ["image/png"],
        "tools": [
            {"name": "read_tool", "risk": "read", "enabled": True},
            {"name": "write_tool", "risk": "write", "enabled": True},
            {"name": "code_tool", "risk": "arbitrary_code", "enabled": True},
            {"name": "blocked_tool", "risk": "read", "enabled": False},
        ],
    }
    payload.update(overrides)
    return payload


class MCPRegistryTests(unittest.TestCase):
    def test_registry_parses_typed_server_configuration(self) -> None:
        registry = MCPServerRegistry.from_payload(
            {"version": 1, "servers": [server_payload()]}
        )
        server = registry.get("test-local")
        self.assertEqual(server.transport, "streamable_http")
        self.assertEqual(server.endpoint, "http://127.0.0.1:8765/mcp")
        self.assertEqual(server.tool_policy("read_tool").risk, RiskClass.READ)  # type: ignore[union-attr]
        self.assertEqual(server.allowed_artifact_mime_types, ("image/png",))

    def test_registry_load_is_machine_file_and_bounded(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "servers.json"
            path.write_text(
                json.dumps({"version": 1, "servers": [server_payload()]}),
                encoding="utf-8",
            )
            registry = MCPServerRegistry.load(path)
        self.assertEqual(registry.source, path)
        self.assertEqual(registry.get("test-local").server_id, "test-local")

    def test_duplicate_server_ids_are_rejected(self) -> None:
        with self.assertRaisesRegex(MCPConfigError, "duplicate server ids"):
            MCPServerRegistry.from_payload(
                {"version": 1, "servers": [server_payload(), server_payload()]}
            )

    def test_duplicate_tool_policies_are_rejected(self) -> None:
        tools = [
            {"name": "same", "risk": "read", "enabled": True},
            {"name": "same", "risk": "write", "enabled": True},
        ]
        with self.assertRaisesRegex(MCPConfigError, "duplicate tool policies"):
            MCPServerRegistry.from_payload(
                {"version": 1, "servers": [server_payload(tools=tools)]}
            )

    def test_non_finite_timeouts_are_rejected(self) -> None:
        for field, value in (
            ("connect_timeout_seconds", float("nan")),
            ("call_timeout_seconds", float("inf")),
        ):
            with self.subTest(field=field):
                with self.assertRaisesRegex(MCPConfigError, "must be finite"):
                    MCPServerRegistry.from_payload(
                        {"version": 1, "servers": [server_payload(**{field: value})]}
                    )

    def test_only_explicit_loopback_hosts_are_accepted(self) -> None:
        self.assertEqual(
            validate_loopback_endpoint("http://localhost:8000/mcp"),
            "http://localhost:8000/mcp",
        )
        self.assertEqual(
            validate_loopback_endpoint("http://[::1]:8000/mcp"),
            "http://[::1]:8000/mcp",
        )
        for endpoint in (
            "http://192.168.1.20:8000/mcp",
            "http://127.0.0.2:8000/mcp",
            "https://example.com/mcp",
            "http://localhost@example.com/mcp",
        ):
            with self.subTest(endpoint=endpoint):
                with self.assertRaises(MCPConfigError):
                    validate_loopback_endpoint(endpoint)

    def test_stdio_transport_is_not_available(self) -> None:
        with self.assertRaisesRegex(MCPConfigError, "streamable_http"):
            MCPServerRegistry.from_payload(
                {
                    "version": 1,
                    "servers": [server_payload(transport="stdio", endpoint="http://localhost/mcp")],
                }
            )

    def test_unknown_and_disabled_servers_fail_closed(self) -> None:
        registry = MCPServerRegistry.from_payload(
            {"version": 1, "servers": [server_payload(enabled=False)]}
        )
        with self.assertRaises(MCPUnknownServerError):
            registry.get("missing")
        with self.assertRaises(MCPServerDisabledError):
            registry.get("test-local")

    def test_unknown_and_disabled_tools_fail_closed(self) -> None:
        server = MCPServerRegistry.from_payload(
            {"version": 1, "servers": [server_payload()]}
        ).get("test-local")
        with self.assertRaises(MCPUnknownToolError):
            authorize_tool(server, "missing", intent=None)
        with self.assertRaises(MCPToolDeniedError):
            authorize_tool(server, "blocked_tool", intent=None)

    def test_write_and_arbitrary_code_require_matching_explicit_intent(self) -> None:
        server = MCPServerRegistry.from_payload(
            {"version": 1, "servers": [server_payload()]}
        ).get("test-local")
        with self.assertRaises(MCPIntentRequiredError):
            authorize_tool(server, "write_tool", intent=None)
        with self.assertRaises(MCPIntentRequiredError):
            authorize_tool(server, "code_tool", intent=None)
        self.assertEqual(
            authorize_tool(server, "write_tool", intent="write").risk,
            RiskClass.WRITE,
        )
        self.assertEqual(
            authorize_tool(server, "code_tool", intent="arbitrary_code").risk,
            RiskClass.ARBITRARY_CODE,
        )

    def test_read_tool_succeeds_without_extra_intent(self) -> None:
        server = MCPServerRegistry.from_payload(
            {"version": 1, "servers": [server_payload()]}
        ).get("test-local")
        self.assertEqual(authorize_tool(server, "read_tool", intent=None).risk, RiskClass.READ)


if __name__ == "__main__":
    unittest.main()
