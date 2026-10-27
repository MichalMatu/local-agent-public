from __future__ import annotations

import json
import os
import subprocess
import sys
import unittest

from local_agent.config import load_memory_limit_config, load_timeout_config


class TimeoutConfigTests(unittest.TestCase):
    def test_defaults_and_overrides(self) -> None:
        defaults = load_timeout_config({})
        self.assertEqual(
            (
                defaults.command_default,
                defaults.command_max,
                defaults.idle_default,
                defaults.idle_max,
                defaults.task_default,
                defaults.task_max,
            ),
            (900, 7200, 300, 3600, 1800, 21600),
        )
        configured = load_timeout_config(
            {
                "LOCAL_AGENT_COMMAND_TIMEOUT_DEFAULT": "3600",
                "LOCAL_AGENT_COMMAND_TIMEOUT_MAX": "7200",
                "LOCAL_AGENT_IDLE_TIMEOUT_DEFAULT": "1200",
                "LOCAL_AGENT_IDLE_TIMEOUT_MAX": "3600",
                "LOCAL_AGENT_TASK_TIMEOUT_DEFAULT": "7200",
                "LOCAL_AGENT_TASK_TIMEOUT_MAX": "21600",
            }
        )
        self.assertEqual(configured.command_default, 3600)
        self.assertEqual(configured.task_default, 7200)

    def test_rejects_malformed_and_non_positive_values(self) -> None:
        for value in ("nope", "0", "-1"):
            with self.subTest(value=value), self.assertRaisesRegex(
                ValueError, "LOCAL_AGENT"
            ):
                load_timeout_config({"LOCAL_AGENT_TASK_TIMEOUT_MAX": value})

    def test_configured_maximum_has_no_hidden_absolute_ceiling(self) -> None:
        configured = load_timeout_config(
            {
                "LOCAL_AGENT_TASK_TIMEOUT_MAX": "1000000",
            }
        )
        self.assertEqual(configured.task_max, 1_000_000)

    def test_rejects_default_above_its_maximum(self) -> None:
        with self.assertRaisesRegex(ValueError, "cannot exceed"):
            load_timeout_config(
                {
                    "LOCAL_AGENT_COMMAND_TIMEOUT_DEFAULT": "7201",
                    "LOCAL_AGENT_COMMAND_TIMEOUT_MAX": "7200",
                }
            )

    def test_stop_grace_is_configurable_and_bounded(self) -> None:
        self.assertEqual(load_timeout_config({}).stop_grace, 300)
        self.assertEqual(
            load_timeout_config({"LOCAL_AGENT_STOP_GRACE_SECONDS": "60"}).stop_grace,
            60,
        )
        for raw in ("0", "-1", "invalid", "3601"):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                load_timeout_config({"LOCAL_AGENT_STOP_GRACE_SECONDS": raw})

    def test_memory_policy_rejects_invalid_bounds(self) -> None:
        self.assertEqual(load_memory_limit_config({}).default_mb, 4096)
        for overrides in (
            {"LOCAL_AGENT_MEMORY_LIMIT_MB_MIN": "-1"},
            {"LOCAL_AGENT_MEMORY_LIMIT_MB_DEFAULT": "invalid"},
            {"LOCAL_AGENT_MEMORY_LIMIT_MB_DEFAULT": "20000"},
            {"LOCAL_AGENT_MEMORY_LIMIT_MB_MAX": "32"},
            {"LOCAL_AGENT_MEMORY_LIMIT_MB_MIN": "8192"},
            {"LOCAL_AGENT_MEMORY_LIMIT_MB_MAX": "0"},
        ):
            with self.subTest(overrides=overrides), self.assertRaises(ValueError):
                load_memory_limit_config(overrides)

    def test_installed_pi_policy_rejects_unlimited_and_oversized_task_requests(self) -> None:
        environment = dict(os.environ)
        environment.update({
            "LOCAL_AGENT_MEMORY_LIMIT_MB_MIN": "1",
            "LOCAL_AGENT_MEMORY_LIMIT_MB_DEFAULT": "32",
            "LOCAL_AGENT_MEMORY_LIMIT_MB_MAX": "64",
        })
        result = subprocess.run([
            sys.executable, '-c',
            'import json\nfrom local_agent.runtime.task_contract import memory_limit_for\n'
            'values = {"default": memory_limit_for({})}\n'
            'for label, limit in [("unlimited", 0), ("oversized", 65)]:\n'
            '    try: memory_limit_for({"memory_limit_mb": limit})\n'
            '    except ValueError: values[label] = "rejected"\n'
            'print(json.dumps(values))\n',
        ], env=environment, capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), {
            "default": 32, "unlimited": "rejected", "oversized": "rejected",
        })


if __name__ == "__main__":
    unittest.main()
