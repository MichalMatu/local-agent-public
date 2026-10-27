from __future__ import annotations

import io
import json
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch
from argparse import Namespace

from local_agent.cli.diagnostics import command_host_inventory
from local_agent.host_ops.capabilities.local.host.models import HostProfile
from local_agent.host_ops.capabilities.local.tools.models import ToolInspectionResult
from local_agent.tool_runtime.host_inventory import preview_host_inventory


class HostInventoryTests(unittest.TestCase):
    @staticmethod
    def profile() -> HostProfile:
        return HostProfile(
            hostname="private-laptop.local", system="Darwin", release="25.5.0",
            architecture="arm64", logical_cpu_count=8, memory_total_bytes=8589934592,
            root_total_bytes=200_000_000, root_free_bytes=50_000_000,
            gpu_devices=("Apple M1", "/Users/private/secret"),
        )

    @staticmethod
    def tool(name="ssh", version="OpenSSH_10.0p1, LibreSSL 3.3.6", error=None):
        return ToolInspectionResult(
            name=name, present=True, resolved_path="/Users/private/bin/ssh",
            version=version, version_exit_code=0 if error is None else 255, error=error,
        )

    def preview(self, **overrides):
        args = {
            "host_id": "worker-mac", "source_revision": "f" * 40,
            "collected_at": "2026-10-12T16:00:00+00:00", "profile": self.profile(),
            "tools": [self.tool(), self.tool(name="pio", version="PlatformIO 6.1.18")],
        }
        return preview_host_inventory(**(args | overrides))

    def test_redacted_sorted_and_non_authoritative(self):
        data = self.preview()
        encoded = json.dumps(data)
        self.assertNotIn("private", encoded)
        self.assertNotIn("/Users", encoded)
        self.assertEqual(data["system"]["os"], "Darwin")
        self.assertEqual(data["system"]["gpu_devices"], ["Apple M1"])
        self.assertEqual([x["name"] for x in data["capabilities"]["tools"]], ["pio", "ssh"])
        self.assertEqual(data["capabilities"]["execution_permission"], "not_asserted")
        self.assertEqual(data["capabilities"]["tools"][1]["version_probe_ok"], True)

    def test_untrusted_version_is_not_published_as_safe(self):
        data = self.preview(tools=[self.tool(version="version /Users/private/secrets")])
        self.assertIsNone(data["capabilities"]["tools"][0]["version"])
        self.assertFalse(data["capabilities"]["tools"][0]["version_probe_ok"])

    def test_failed_probe_does_not_imply_readiness(self):
        data = self.preview(tools=[self.tool(error="bad command with secret path")])
        self.assertEqual(data["capabilities"]["tools"][0]["version_probe_ok"], False)
        self.assertNotIn("bad command", json.dumps(data))

    def test_fail_closed_on_identity_and_data_errors(self):
        cases = [
            {"host_id": "../worker"}, {"host_id": "Worker"},
            {"source_revision": "main"}, {"collected_at": "2026-10-12"},
            {"tools": [self.tool(), self.tool()]},
            {"tools": [self.tool(name="../ssh")]},
            {"tools": [ToolInspectionResult(name="git", present="false")]},
            {"tools": [self.tool()] * 65},
        ]
        for invalid in cases:
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                self.preview(**invalid)

    def test_cli_reports_preview_without_publishing_or_authorizing(self):
        args = Namespace(host_id="worker-mac", source_revision="f" * 40, tools=["ssh"])
        sink = io.StringIO()
        with (
            patch("local_agent.cli.diagnostics.HostProfiler") as profiler,
            patch("local_agent.cli.diagnostics.ToolInspector") as inspector,
            redirect_stdout(sink),
        ):
            profiler.return_value.inspect.return_value = self.profile()
            inspector.return_value.inspect.return_value = [self.tool()]
            self.assertEqual(command_host_inventory(args), 0)
        payload = json.loads(sink.getvalue())
        self.assertEqual(payload["capabilities"]["tools"][0]["name"], "ssh")
        self.assertEqual(payload["system"]["record_type"], "diagnostic_preview")


if __name__ == "__main__":
    unittest.main()
