from __future__ import annotations

import contextlib
import io
import unittest
import sys
import threading
from unittest import mock

import local_agent.supervisor.serial as multi
import local_agent.repository.worker as worker
from local_agent.supervisor import control as supervisor_control


class QuietMultiRepositoryLoggingTests(unittest.TestCase):
    def test_control_sync_preserves_task_output_and_degraded_diagnostics(self) -> None:
        for sync in (multi.sync_control_quietly, worker.sync_control_quietly):
            stream = io.StringIO()
            started = threading.Event()
            release = threading.Event()

            def wait_for_sync(_core, *, tasks_dir):
                self.assertTrue(tasks_dir.endswith("/tasks"))
                started.set()
                release.wait(timeout=5)
                print("control sync degraded")

            with self.subTest(sync=sync.__module__), mock.patch.object(
                supervisor_control.storage, "sync_control", side_effect=wait_for_sync
            ), contextlib.redirect_stdout(stream):
                thread = threading.Thread(target=sync)
                thread.start()
                try:
                    self.assertTrue(started.wait(timeout=5))
                    self.assertIs(sys.stdout, stream)
                    print("concurrent task output")
                finally:
                    release.set()
                    thread.join(timeout=5)
                self.assertFalse(thread.is_alive())
            self.assertEqual(stream.getvalue(), "concurrent task output\ncontrol sync degraded\n")

    def test_idle_summary_is_one_compact_line(self) -> None:
        self.assertEqual(multi.format_idle_summary(3), "no pending task (3 repositories)")
        self.assertEqual(multi.format_idle_summary(1), "no pending task (1 repository)")


if __name__ == "__main__":
    unittest.main()
