from __future__ import annotations

import unittest
from unittest import mock

from local_agent.runtime import output, telemetry


class RuntimeModuleBoundaryTests(unittest.TestCase):
    def test_rss_sampler_uses_telemetry_command(self) -> None:
        ps_output = "1 77 1.0 1024\n2 77 2.0 3072\n"
        with mock.patch.object(telemetry, "_safe_command", return_value=ps_output):
            self.assertEqual(telemetry.sample_process_group_rss_mb(77), 4.0)

    def test_extracted_telemetry_module_works_independently(self) -> None:
        parsed = telemetry.parse_process_group_ps(
            "1 7 2.0 1024\n2 7 3.0 2048\n", 7
        )
        self.assertEqual(parsed["command_cpu_percent"], 5.0)
        self.assertEqual(parsed["command_children"], 1)
        output.LiveCommandOutput().finish()


if __name__ == "__main__":
    unittest.main()
