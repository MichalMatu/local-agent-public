from __future__ import annotations

import asyncio
import base64
import hashlib
import http.server
import socket
import tempfile
import threading
import time
import unittest
from pathlib import Path

import uvicorn
from mcp import types
from mcp.server import MCPServer
from mcp.server.mcpserver import Image

from local_agent.mcp.client import call_tool, discover_tools
from local_agent.mcp.config import MAX_ARGUMENT_BYTES
from local_agent.mcp.errors import (
    MCPArgumentsError,
    MCPArtifactError,
    MCPCallTimeoutError,
    MCPConnectionTimeoutError,
    MCPResultTooLargeError,
    MCPTransportError,
)
from local_agent.mcp.registry import MCPServerRegistry


PNG_BYTES = b"\x89PNG\r\n\x1a\nlocal-agent-mcp"
BLOB_BYTES = b"local-agent-mcp-blob"


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _server_record(
    port: int,
    *,
    server_id: str = "http-test",
    connect_timeout: float = 1.0,
    call_timeout: float = 1.0,
    max_text_bytes: int = 32768,
    max_artifact_bytes: int = 4096,
    allowed_mimes: list[str] | None = None,
) -> dict[str, object]:
    return {
        "id": server_id,
        "transport": "streamable_http",
        "endpoint": f"http://127.0.0.1:{port}/mcp",
        "enabled": True,
        "connect_timeout_seconds": connect_timeout,
        "call_timeout_seconds": call_timeout,
        "max_text_bytes": max_text_bytes,
        "max_artifact_bytes": max_artifact_bytes,
        "max_tools": 32,
        "max_artifacts": 4,
        "allowed_artifact_mime_types": (
            ["image/png", "application/octet-stream"]
            if allowed_mimes is None
            else allowed_mimes
        ),
        "tools": [
            {"name": "read_tool", "risk": "read", "enabled": True},
            {"name": "write_tool", "risk": "write", "enabled": True},
            {"name": "code_tool", "risk": "arbitrary_code", "enabled": True},
            {"name": "slow_tool", "risk": "read", "enabled": True},
            {"name": "large_tool", "risk": "read", "enabled": True},
            {"name": "image_tool", "risk": "read", "enabled": True},
            {"name": "blob_tool", "risk": "read", "enabled": True},
        ],
    }


def _registry(record: dict[str, object]) -> MCPServerRegistry:
    return MCPServerRegistry.from_payload({"version": 1, "servers": [record]})


class _QuietHandler(http.server.BaseHTTPRequestHandler):
    response_body = b"not-json"
    delay = 0.0

    def do_POST(self) -> None:  # noqa: N802
        if self.delay:
            time.sleep(self.delay)
        length = int(self.headers.get("Content-Length", "0"))
        if length:
            self.rfile.read(length)
        try:
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(self.response_body)))
            self.end_headers()
            self.wfile.write(self.response_body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def log_message(self, _format: str, *_args: object) -> None:
        return


class _RawHTTPServer:
    def __init__(self, *, delay: float = 0.0) -> None:
        handler = type("Handler", (_QuietHandler,), {"delay": delay})
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.port = int(self.server.server_address[1])
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)


class MCPHTTPIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        mcp = MCPServer("Local Agent Hermetic MCP")

        @mcp.tool()
        def read_tool(value: str = "ok") -> str:
            """Return read-only text."""
            return f"read:{value}"

        @mcp.tool()
        def write_tool(value: str = "ok") -> str:
            """A policy-classified write test tool."""
            return f"write:{value}"

        @mcp.tool()
        def code_tool(value: str = "ok") -> str:
            """A policy-classified arbitrary-code test tool."""
            return f"code:{value}"

        @mcp.tool()
        async def slow_tool() -> str:
            """Sleep long enough to exercise the client call timeout."""
            await asyncio.sleep(0.4)
            return "slow"

        @mcp.tool()
        def large_tool() -> str:
            """Return an intentionally oversized textual result."""
            return "x" * 10000

        @mcp.tool()
        def image_tool() -> Image:
            """Return a small binary image block."""
            return Image(data=PNG_BYTES, format="png")

        @mcp.tool()
        def blob_tool() -> types.EmbeddedResource:
            """Return a small embedded binary resource."""
            return types.EmbeddedResource(
                type="resource",
                resource=types.BlobResourceContents(
                    uri="test://local-agent/blob",
                    mime_type="application/octet-stream",
                    blob=base64.b64encode(BLOB_BYTES).decode("ascii"),
                ),
            )

        cls.mcp = mcp
        cls.port = _free_port()
        cls.uvicorn = uvicorn.Server(
            uvicorn.Config(
                mcp.streamable_http_app(json_response=True, stateless_http=True),
                host="127.0.0.1",
                port=cls.port,
                log_level="critical",
            )
        )
        cls.thread = threading.Thread(target=cls.uvicorn.run, daemon=True)
        cls.thread.start()
        deadline = time.monotonic() + 5
        while not cls.uvicorn.started and time.monotonic() < deadline:
            time.sleep(0.01)
        if not cls.uvicorn.started:
            raise RuntimeError("hermetic MCP HTTP server did not start")

    @classmethod
    def tearDownClass(cls) -> None:
        cls.uvicorn.should_exit = True
        cls.thread.join(timeout=5)

    def test_real_http_tools_discovery_and_sdk_protocol_negotiation(self) -> None:
        result = asyncio.run(discover_tools(_registry(_server_record(self.port)), "http-test"))
        self.assertTrue(result["ok"])
        self.assertRegex(result["protocol_version"], r"^\d{4}-\d{2}-\d{2}$")
        names = {tool["name"] for tool in result["tools"]}
        self.assertTrue({"read_tool", "write_tool", "image_tool", "blob_tool"}.issubset(names))
        policies = {tool["name"]: tool["local_policy"] for tool in result["tools"]}
        self.assertEqual(policies["write_tool"]["risk"], "write")

    def test_normal_read_only_tool_invocation(self) -> None:
        result = asyncio.run(
            call_tool(
                _registry(_server_record(self.port)),
                "http-test",
                "read_tool",
                {"value": "hello"},
            )
        )
        self.assertTrue(result["ok"])
        self.assertEqual(result["risk"], "read")
        self.assertTrue(any(item.get("text") == "read:hello" for item in result["content"]))

    def test_programmatic_arguments_are_bounded_before_network(self) -> None:
        registry = _registry(_server_record(_free_port()))
        with self.assertRaisesRegex(MCPArgumentsError, "arguments exceed"):
            asyncio.run(
                call_tool(
                    registry,
                    "http-test",
                    "read_tool",
                    {"value": "x" * MAX_ARGUMENT_BYTES},
                )
            )

    def test_programmatic_arguments_must_be_json_serializable(self) -> None:
        registry = _registry(_server_record(_free_port()))
        with self.assertRaisesRegex(MCPArgumentsError, "not JSON-serializable"):
            asyncio.run(
                call_tool(
                    registry,
                    "http-test",
                    "read_tool",
                    {"value": object()},
                )
            )

    def test_write_and_arbitrary_code_succeed_only_with_matching_intent(self) -> None:
        registry = _registry(_server_record(self.port))
        write = asyncio.run(
            call_tool(registry, "http-test", "write_tool", {"value": "x"}, intent="write")
        )
        code = asyncio.run(
            call_tool(
                registry,
                "http-test",
                "code_tool",
                {"value": "x"},
                intent="arbitrary_code",
            )
        )
        self.assertTrue(write["ok"])
        self.assertTrue(code["ok"])

    def test_call_timeout_is_bounded(self) -> None:
        registry = _registry(_server_record(self.port, call_timeout=0.1))
        with self.assertRaises(MCPCallTimeoutError):
            asyncio.run(call_tool(registry, "http-test", "slow_tool", {}))

    def test_oversized_textual_result_is_rejected(self) -> None:
        registry = _registry(_server_record(self.port, max_text_bytes=4096))
        with self.assertRaises(MCPResultTooLargeError):
            asyncio.run(call_tool(registry, "http-test", "large_tool", {}))

    def test_image_result_is_persisted_as_bounded_artifact(self) -> None:
        registry = _registry(_server_record(self.port))
        with tempfile.TemporaryDirectory() as tmp:
            result = asyncio.run(
                call_tool(
                    registry,
                    "http-test",
                    "image_tool",
                    {},
                    artifact_dir=Path(tmp),
                )
            )
            image = next(item for item in result["content"] if item["type"] == "image")
            metadata = image["artifact"]
            path = Path(metadata["path"])
            digest = hashlib.sha256(PNG_BYTES).hexdigest()
            self.assertTrue(path.is_file())
            self.assertEqual(path.read_bytes(), PNG_BYTES)
            self.assertEqual(metadata["mime_type"], "image/png")
            self.assertEqual(metadata["size"], len(PNG_BYTES))
            self.assertEqual(metadata["sha256"], digest)
            self.assertTrue(path.name.endswith(f"--{digest}.png"))
            self.assertNotIn("data", image)

    def test_blob_resource_is_persisted_without_base64_in_output(self) -> None:
        registry = _registry(_server_record(self.port))
        with tempfile.TemporaryDirectory() as tmp:
            result = asyncio.run(
                call_tool(
                    registry,
                    "http-test",
                    "blob_tool",
                    {},
                    artifact_dir=Path(tmp),
                )
            )
            blob = next(item for item in result["content"] if item["type"] == "resource_blob")
            metadata = blob["artifact"]
            path = Path(metadata["path"])
            self.assertEqual(blob["uri"], "test://local-agent/blob")
            self.assertEqual(path.read_bytes(), BLOB_BYTES)
            self.assertEqual(metadata["mime_type"], "application/octet-stream")
            self.assertEqual(metadata["size"], len(BLOB_BYTES))
            self.assertEqual(metadata["sha256"], hashlib.sha256(BLOB_BYTES).hexdigest())
            self.assertNotIn("blob", blob)

    def test_disallowed_artifact_mime_is_rejected_before_persistence(self) -> None:
        registry = _registry(
            _server_record(self.port, allowed_mimes=["application/octet-stream"])
        )
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(MCPArtifactError):
                asyncio.run(
                    call_tool(
                        registry,
                        "http-test",
                        "image_tool",
                        {},
                        artifact_dir=Path(tmp),
                    )
                )
            self.assertEqual(list(Path(tmp).iterdir()), [])

    def test_image_size_limit_is_fail_closed_before_persistence(self) -> None:
        registry = _registry(_server_record(self.port, max_artifact_bytes=4))
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(MCPResultTooLargeError):
                asyncio.run(
                    call_tool(
                        registry,
                        "http-test",
                        "image_tool",
                        {},
                        artifact_dir=Path(tmp),
                    )
                )
            self.assertEqual(list(Path(tmp).iterdir()), [])

    def test_connection_timeout_is_bounded(self) -> None:
        raw = _RawHTTPServer(delay=0.4)
        try:
            record = _server_record(
                raw.port,
                server_id="slow-connect",
                connect_timeout=0.1,
            )
            with self.assertRaises(MCPConnectionTimeoutError):
                asyncio.run(discover_tools(_registry(record), "slow-connect"))
        finally:
            raw.close()

    def test_malformed_mcp_response_is_rejected(self) -> None:
        raw = _RawHTTPServer()
        try:
            record = _server_record(raw.port, server_id="malformed")
            with self.assertRaises(MCPTransportError):
                asyncio.run(discover_tools(_registry(record), "malformed"))
        finally:
            raw.close()


if __name__ == "__main__":
    unittest.main()
