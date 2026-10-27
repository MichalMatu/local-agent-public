from __future__ import annotations

import io
import queue
import shlex
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path

import local_agent.foundation.core as core
from local_agent.foundation.process import (
    spawn_argv,
    start_output_pump,
    unregister_process,
)
from local_agent.runtime.executor import RuntimeExecutor


class IdleOutputActivityTests(unittest.TestCase):
    def test_partial_line_output_keeps_idle_watchdog_alive_without_phantom_output(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            original_work = core.WORK
            core.WORK = Path(tmp)
            try:
                runtime = RuntimeExecutor(core, rss_sampler=lambda _pgid: None)
                runtime._idle_timeout = 1
                runtime._memory_limit_mb = 0
                runtime._deadline = time.monotonic() + 120
                runtime._command_count = 1
                runtime._primary_count = 1
                script = (
                    "import sys,time\n"
                    "for _ in range(6):\n"
                    "    sys.stdout.write('x')\n"
                    "    sys.stdout.flush()\n"
                    "    time.sleep(0.3)\n"
                )
                command = f"{shlex.quote(sys.executable)} -c {shlex.quote(script)}"
                live = io.StringIO()
                with redirect_stdout(live):
                    result = runtime.run_command(command, 5)
            finally:
                core.WORK = original_work

        self.assertEqual(result["exit_code"], 0)
        self.assertFalse(result["idle_timed_out"])
        self.assertEqual(result["output"], "xxxxxx")
        self.assertGreater(result["elapsed_seconds"], 1.0)
        self.assertEqual(
            live.getvalue().count("[CMD] "),
            1,
            "raw-byte activity must not create synthetic queue/output records",
        )

    def test_output_pump_preserves_universal_newlines_across_raw_chunks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            script = "import os; os.write(1, b'a\\rb\\r\\nc\\nd')"
            proc = spawn_argv(
                [sys.executable, "-c", script],
                cwd=Path(tmp),
                env=core.ENV,
            )
            pump = start_output_pump(proc, read_size=1)
            chunks: list[str] = []
            try:
                while True:
                    item = pump.queue.get(timeout=5)
                    if item is None:
                        break
                    chunks.append(item)
                proc.wait(timeout=5)
            finally:
                if proc.poll() is None:
                    core.kill_process_group(proc)
                pump.stop()
                unregister_process(proc)

        self.assertTrue(chunks)
        self.assertNotIn("", chunks)
        self.assertEqual("".join(chunks), "a\nb\nc\nd")

    def test_partial_bytes_update_activity_without_entering_output_queue(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            script = (
                "import sys,time\n"
                "sys.stdout.write('x')\n"
                "sys.stdout.flush()\n"
                "time.sleep(0.8)\n"
            )
            proc = spawn_argv(
                [sys.executable, "-c", script],
                cwd=Path(tmp),
                env=core.ENV,
            )
            pump = start_output_pump(proc)
            initial = pump.activity_at()
            try:
                deadline = time.monotonic() + 1.0
                while pump.activity_at() <= initial and time.monotonic() < deadline:
                    time.sleep(0.02)
                self.assertGreater(pump.activity_at(), initial)
                with self.assertRaises(queue.Empty):
                    pump.queue.get_nowait()

                proc.wait(timeout=5)
                output: list[str] = []
                while True:
                    item = pump.queue.get(timeout=5)
                    if item is None:
                        break
                    output.append(item)
                self.assertEqual(output, ["x"])
            finally:
                if proc.poll() is None:
                    core.kill_process_group(proc)
                pump.stop()
                unregister_process(proc)


if __name__ == "__main__":
    unittest.main()
