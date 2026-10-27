from __future__ import annotations

import io
import shlex
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

import local_agent.foundation.core as core
from local_agent.runtime import telemetry
from local_agent.runtime.task_contract import (
    DEFAULT_MEMORY_LIMIT_MB,
    MAX_MEMORY_LIMIT_MB,
    memory_limit_for,
)
from local_agent.runtime.executor import (
    MAX_OUTPUT,
    RuntimeExecutor,
)
from local_agent.runtime.telemetry import (
    sample_process_group_rss_mb,
)


class RuntimeHardeningTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.original_work = core.WORK
        core.WORK = Path(self.tmp.name)

    def tearDown(self) -> None:
        core.WORK = self.original_work
        self.tmp.cleanup()

    def test_memory_limit_defaults_bounds_and_disable(self) -> None:
        self.assertEqual(memory_limit_for({}), DEFAULT_MEMORY_LIMIT_MB)
        self.assertEqual(memory_limit_for({"memory_limit_mb": 0}), 0)
        self.assertEqual(
            memory_limit_for({"memory_limit_mb": MAX_MEMORY_LIMIT_MB}),
            MAX_MEMORY_LIMIT_MB,
        )
        with self.assertRaises(ValueError):
            memory_limit_for({"memory_limit_mb": MAX_MEMORY_LIMIT_MB + 1})

    def test_process_group_rss_sampler_uses_existing_telemetry_parser(self) -> None:
        ps_output = "1 77 1.0 1024\n2 77 2.0 3072\n3 88 9.0 9999\n"
        with mock.patch.object(telemetry, "_safe_command", return_value=ps_output):
            self.assertEqual(sample_process_group_rss_mb(77), 4.0)

    def test_large_runtime_output_capture_is_strictly_bounded(self) -> None:
        runtime = RuntimeExecutor(core, rss_sampler=lambda _pgid: None)
        runtime._idle_timeout = 5
        runtime._memory_limit_mb = 0
        runtime._deadline = time.monotonic() + 120
        runtime._command_count = 1
        runtime._primary_count = 1
        command = f"{shlex.quote(sys.executable)} -c " + shlex.quote(
            "import sys; sys.stdout.write('x' * 200000)"
        )
        with redirect_stdout(io.StringIO()):
            result = runtime.run_command(command, 5)
        self.assertEqual(result["exit_code"], 0)
        self.assertEqual(len(result["output"]), MAX_OUTPUT)
        self.assertEqual(result["output"], "x" * MAX_OUTPUT)

    def test_large_core_output_capture_is_strictly_bounded(self) -> None:
        command = f"{shlex.quote(sys.executable)} -c " + shlex.quote(
            "import sys; sys.stdout.write('y' * 200000)"
        )
        with redirect_stdout(io.StringIO()):
            result = core.run_command(command, 5)
        self.assertEqual(result["exit_code"], 0)
        self.assertEqual(len(result["output"]), core.MAX_OUTPUT)
        self.assertEqual(result["output"], "y" * core.MAX_OUTPUT)

    def test_memory_watchdog_kills_after_two_over_limit_samples(self) -> None:
        runtime = RuntimeExecutor(
            core,
            rss_sampler=lambda _pgid: 2.0,
            memory_sample_interval=0.05,
        )
        runtime._idle_timeout = 0
        runtime._memory_limit_mb = 1
        runtime._deadline = time.monotonic() + 120
        runtime._command_count = 1
        runtime._primary_count = 1
        command = f"{shlex.quote(sys.executable)} -c " + shlex.quote(
            "import time; time.sleep(5)"
        )
        with redirect_stdout(io.StringIO()):
            result = runtime.run_command(command, 5)
        self.assertEqual(result["exit_code"], 124)
        self.assertTrue(result["memory_limited"])
        self.assertEqual(result["failure_reason"], "command_memory_limit")
        self.assertEqual(result["peak_rss_mb"], 2.0)

    def test_runtime_injects_runner_without_mutating_core_global(self) -> None:
        runtime = RuntimeExecutor(core)
        original_runner = core.run_command
        task = {"id": "runner-injection", "resources": [], "commands": []}
        with mock.patch.object(
            core,
            "process_task",
            return_value={"status": "done", "finished_at": "done"},
        ) as process_task:
            result = runtime.process_task(task)

        self.assertEqual(result["status"], "done")
        self.assertIs(core.run_command, original_runner)
        injected = process_task.call_args.kwargs["command_runner"]
        self.assertIs(injected.__self__, runtime)
        self.assertIs(injected.__func__, runtime.run_command.__func__)


if __name__ == "__main__":
    unittest.main()
