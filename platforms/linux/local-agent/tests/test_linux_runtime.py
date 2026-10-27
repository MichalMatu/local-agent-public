"""Offline Linux Core smoke and safety regressions, no GitHub or service required."""
from __future__ import annotations

# ruff: noqa: E402 -- local source import setup in standalone test
import copy
import pathlib
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from linux_agent.runtime.task_contract import task_digest
from linux_agent.runtime.core import Core
from linux_agent.runtime.executor import RuntimeExecutor
from linux_agent.runtime.telemetry import sample_process_group_rss_mb


class LinuxRuntimeTests(unittest.TestCase):
    def task(self, cwd: pathlib.Path, command: str = "printf hello") -> dict:
        return {"version": 1, "id": "sample-a", "target_host_id": "debian-n5105",
                "target_installation_id": "123e4567-e89b-12d3-a456-426614174000",
                "cwd": str(cwd), "commands": [command], "resources": [],
                "command_timeout": 5, "task_timeout": 75, "idle_timeout": 5,
                "memory_limit_mb": 256, "output_policy": "summary"}

    def test_exec_and_digest(self):
        with tempfile.TemporaryDirectory() as tmp:
            task = self.task(pathlib.Path(tmp))
            self.assertEqual(task_digest(task), task_digest(copy.deepcopy(task)))
            result = RuntimeExecutor(Core(pathlib.Path(tmp))).process_task(task)
            self.assertEqual(result["status"], "done")
            self.assertEqual(result["commands"][0]["exit_code"], 0)
            self.assertIn("hello", result["commands"][0]["output"])

    def test_wrong_schema_fails_before_effects(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp)
            task = self.task(path)
            task["commands"] = ["touch " + str(path / "marker")]
            task["resources"] = ["machine", "usb"]
            with self.assertRaises(ValueError):
                RuntimeExecutor(Core(path)).process_task(task)
            self.assertFalse((path / "marker").exists())

    def test_timeout_process_group(self):
        with tempfile.TemporaryDirectory() as tmp:
            task = self.task(pathlib.Path(tmp), "sleep 3")
            task["command_timeout"] = 1
            task["task_timeout"] = 65
            result = RuntimeExecutor(Core(pathlib.Path(tmp))).process_task(task)
            self.assertEqual(result["status"], "failed")
            self.assertTrue(result["commands"][0]["timed_out"])

    def test_linux_telemetry(self):
        self.assertIsNone(sample_process_group_rss_mb(-123456789))


if __name__ == "__main__":
    unittest.main()
