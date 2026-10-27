"""Offline Debian port regression: Mac-derived contract/process runtime, no service."""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path

from linux_agent.foundation.core import HostCore
from linux_agent.runtime.executor import RuntimeExecutor
from linux_agent.runtime.task_contract import task_digest, validate_task
from linux_agent.runtime.telemetry import sample_process_group_rss_mb


class PortTests(unittest.TestCase):
    def task(self, command="printf 'LINUX_CORE_OK\\n'"):
        return {
            "id": "linux-port-test", "commands": [command], "resources": [],
            "command_timeout": 3, "task_timeout": 90,
            "idle_timeout": 10, "memory_limit_mb": 128,
        }

    def test_contract_digest_exact_json_wire(self):
        task = self.task()
        expected = hashlib.sha256(
            json.dumps(task, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
        ).hexdigest()
        self.assertEqual(task_digest(task), expected)
        validate_task(task)

    def test_command_executes_native_without_mac_import(self):
        with tempfile.TemporaryDirectory() as directory:
            task = self.task()
            result = RuntimeExecutor(HostCore(Path(directory), dict(os.environ))).process_task(task)
            self.assertEqual(result["status"], "done")
            self.assertEqual(result["commands"][0]["exit_code"], 0)
            self.assertIn("LINUX_CORE_OK", result["commands"][0]["output"])
            self.assertFalse(any(key.startswith("local_agent.") for key in __import__("sys").modules))

    def test_invalid_limits_fail_closed(self):
        task = self.task()
        task["command_timeout"] = True
        with self.assertRaises(ValueError):
            validate_task(task)
        task = self.task()
        task["resources"] = ["disk", "disk"]
        with self.assertRaises(ValueError):
            validate_task(task)
        task = self.task()
        task["task_timeout"] = 5
        with self.assertRaises(ValueError):
            validate_task(task)

    def test_timeout_terminates_group(self):
        with tempfile.TemporaryDirectory() as directory:
            task = self.task("sleep 6")
            task["command_timeout"] = 1
            result = RuntimeExecutor(HostCore(Path(directory), dict(os.environ))).process_task(task)
            self.assertEqual(result["status"], "failed")
            self.assertTrue(result["commands"][0]["timed_out"])

    def test_linux_proc_sampler(self):
        import os
        self.assertIsInstance(sample_process_group_rss_mb(os.getpgrp()), float)


if __name__ == "__main__":
    unittest.main()
