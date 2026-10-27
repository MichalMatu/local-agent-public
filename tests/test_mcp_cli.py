from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


def _write_registry(path: Path) -> None:
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "servers": [
                    {
                        "id": "cli-test",
                        "transport": "streamable_http",
                        "endpoint": "http://127.0.0.1:8765/mcp",
                        "enabled": True,
                        "tools": [
                            {"name": "read_tool", "risk": "read", "enabled": True}
                        ],
                        "allowed_artifact_mime_types": [],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )


class MCPCLITests(unittest.TestCase):
    def test_servers_command_uses_explicit_machine_registry_and_emits_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            registry = Path(tmp) / "servers.json"
            _write_registry(registry)
            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "local_agent.mcp.cli",
                    "--registry",
                    str(registry),
                    "servers",
                ],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["registry"], str(registry))
        self.assertEqual(payload["servers"][0]["id"], "cli-test")
        self.assertEqual(payload["servers"][0]["endpoint"], "http://127.0.0.1:8765/mcp")

    def test_call_rejects_non_object_arguments_with_specific_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            registry = Path(tmp) / "servers.json"
            _write_registry(registry)
            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "local_agent.mcp.cli",
                    "--registry",
                    str(registry),
                    "call",
                    "cli-test",
                    "read_tool",
                    "--arguments",
                    "[]",
                ],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
        self.assertEqual(result.returncode, 2, result.stderr)
        payload = json.loads(result.stdout)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error"]["code"], "invalid_arguments")

    def test_help_is_packaged_and_executable(self) -> None:
        result = subprocess.run(
            [sys.executable, "-m", "local_agent.mcp.cli", "--help"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Local Agent local MCP client", result.stdout)
        self.assertIn("servers", result.stdout)
        self.assertIn("tools", result.stdout)
        self.assertIn("call", result.stdout)

    def test_call_help_exposes_artifact_directory_option(self) -> None:
        result = subprocess.run(
            [sys.executable, "-m", "local_agent.mcp.cli", "call", "--help"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--artifact-dir", result.stdout)
        self.assertIn("--intent", result.stdout)


if __name__ == "__main__":
    unittest.main()
