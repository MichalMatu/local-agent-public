from __future__ import annotations

import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from local_agent.repository.context import RepositoryContext
from local_agent.repository import worker

ROOT = Path(__file__).resolve().parents[1]


class SerialShutdownTests(unittest.TestCase):
    def run_signal_case(self, signum: int, duration: float) -> tuple[int, bool, float]:
        with tempfile.TemporaryDirectory() as folder:
            started, completed = (Path(folder) / name for name in ('started', 'completed'))
            child = (
                'import time; from pathlib import Path; '
                f'Path({str(started)!r}).write_text("started"); '
                f'time.sleep({duration!r}); Path({str(completed)!r}).write_text("completed")'
            )
            harness = f'''
import contextlib
from pathlib import Path
from unittest import mock
from local_agent.supervisor import serial
serial.install_signal_handlers()
with mock.patch.object(serial, 'worker_command', return_value=[{sys.executable!r}, '-c', {child!r}]), mock.patch.object(serial, 'repository_execution_lease', return_value=contextlib.nullcontext()):
    raise SystemExit(serial.run_worker(mock.Mock(repository_id='fixture'), registry_path=None))
'''
            env = {**os.environ, 'LOCAL_AGENT_STOP_GRACE_SECONDS': '1'}
            proc = subprocess.Popen(
                [sys.executable, '-c', harness], cwd=ROOT, env=env,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                start_new_session=True,
            )
            try:
                deadline = time.monotonic() + 5
                while not started.exists():
                    if proc.poll() is not None or time.monotonic() >= deadline:
                        output = proc.communicate(timeout=2)[0]
                        self.fail(f'worker fixture did not start: {output[-2000:]}')
                    time.sleep(0.01)
                requested_at = time.monotonic()
                proc.send_signal(signum)
                output = proc.communicate(timeout=6)[0]
                elapsed = time.monotonic() - requested_at
                self.assertLess(len(output), 10000)
                return proc.returncode, completed.exists(), elapsed
            finally:
                if proc.poll() is None:
                    os.killpg(proc.pid, signal.SIGKILL)
                    proc.wait(timeout=2)

    def test_real_sigterm_allows_current_worker_to_finish(self) -> None:
        code, completed, _elapsed = self.run_signal_case(signal.SIGTERM, 0.25)
        self.assertEqual(code, 0)
        self.assertTrue(completed)

    def test_sigterm_during_registered_spawn_keeps_worker_ownership_and_grace(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            completed = Path(folder) / 'completed'
            child = (
                'import time; from pathlib import Path; time.sleep(0.15); '
                f'Path({str(completed)!r}).write_text("completed")'
            )
            harness = f'''
import contextlib, signal
from unittest import mock
from local_agent.foundation import process
from local_agent.supervisor import serial
serial.install_signal_handlers()
original = process.subprocess.Popen
def spawn_then_stop(*args, **kwargs):
    proc = original(*args, **kwargs)
    serial.shutdown_handler(signal.SIGTERM, None)
    return proc
with mock.patch.object(process.subprocess, 'Popen', side_effect=spawn_then_stop), mock.patch.object(serial, 'worker_command', return_value=[{sys.executable!r}, '-c', {child!r}]), mock.patch.object(serial, 'repository_execution_lease', return_value=contextlib.nullcontext()):
    code = serial.run_worker(mock.Mock(repository_id='fixture'), registry_path=None)
assert serial._stop_requested.is_set()
assert not process.process_shutdown_requested()
raise SystemExit(code)
'''
            result = subprocess.run(
                [sys.executable, '-c', harness], cwd=ROOT,
                env={**os.environ, 'LOCAL_AGENT_STOP_GRACE_SECONDS': '1'},
                capture_output=True, text=True, timeout=8,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertTrue(completed.exists())

    def test_real_sigterm_interrupts_after_grace(self) -> None:
        code, completed, elapsed = self.run_signal_case(signal.SIGTERM, 20)
        self.assertEqual(code, 128 + signal.SIGTERM)
        self.assertFalse(completed)
        self.assertGreaterEqual(elapsed, 0.9)
        self.assertLess(elapsed, 5)

    def test_real_sigint_does_not_wait_for_completion_grace(self) -> None:
        code, completed, elapsed = self.run_signal_case(signal.SIGINT, 20)
        self.assertEqual(code, 128 + signal.SIGINT)
        self.assertFalse(completed)
        self.assertLess(elapsed, 0.9)

    def test_host_worker_honors_global_disable_before_syncing_repository_controls(self) -> None:
        repo = RepositoryContext(
            repository_id='local-agent', repository='example-owner/local-agent',
            control=Path('/fixture/control'), work=Path('/fixture/work'),
            checkpoints=Path('/fixture/checkpoints'), host_id='pi-zero2w',
            host_mailbox_enabled=True,
        )
        watcher = worker.ActiveRepositoryControlWatcher(repo, 'fixture-task')
        watcher._stop = mock.Mock()
        watcher._stop.wait.return_value = False
        with mock.patch.object(worker.remote_operator, 'poll_remote_operator') as poll, mock.patch.object(worker.agent_operator, 'is_disabled', return_value=True), mock.patch.object(worker.os, 'kill') as kill, mock.patch.object(worker, 'sync_control_quietly') as sync:
            watcher._run()
        poll.assert_called_once()
        kill.assert_called_once_with(os.getpid(), signal.SIGTERM)
        sync.assert_not_called()


if __name__ == '__main__':
    unittest.main()
