from __future__ import annotations

import os
import signal
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path

from tests.test_multirepo_crash_recovery import process_alive, wait_for_process_exit
from tests.test_multirepo_integration import REPO_ROOT


class GuardProcessTests(unittest.TestCase):
    def test_guard_sigterm_and_unexpected_error_reap_registered_supervisor(self) -> None:
        for failure in ("sigterm", "exception"):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                child_pid_file = root / "child.pid"
                ready = root / "ready"
                child_code = f"import os,time; from pathlib import Path; Path({str(child_pid_file)!r}).write_text(str(os.getpid())); time.sleep(30)"
                code = textwrap.dedent(f"""
                    import sys
                    from pathlib import Path
                    from local_agent import entrypoint
                    from local_agent.foundation import process
                    entrypoint.agent_remote_operator.poll_remote_operator = lambda *_a, **_k: None
                    entrypoint.agent_operator.is_disabled = lambda: False
                    entrypoint.load_repository_registry = lambda **_k: [object()]
                    entrypoint.prepare_repositories = lambda _r: None
                    entrypoint._recover_before_supervisor_start = lambda _r: None
                    entrypoint.supervisor_command = lambda _a: [sys.executable, '-c', {child_code!r}]
                    calls = 0
                    def revision():
                        global calls
                        calls += 1
                        if calls > 2:
                            Path({str(ready)!r}).touch()
                            if {failure!r} == 'exception':
                                raise RuntimeError('injected guard failure')
                        return 'stable'
                    entrypoint.agentd.self_revision = revision
                    try:
                        entrypoint.main()
                    except RuntimeError as exc:
                        assert str(exc) == 'injected guard failure'
                    assert not process._active_processes
                """)
                with (root / "guard.log").open("w+") as log:
                    guard = subprocess.Popen(
                        [sys.executable, "-c", code],
                        cwd=REPO_ROOT,
                        env=dict(os.environ, HOME=tmp),
                        stdout=log,
                        stderr=subprocess.STDOUT,
                        start_new_session=True,
                    )
                    child_pid = None
                    try:
                        deadline = time.monotonic() + 10
                        while not ready.exists() and time.monotonic() < deadline:
                            time.sleep(0.05)
                        self.assertTrue(ready.exists())
                        child_pid = int(child_pid_file.read_text())
                        if failure == "sigterm":
                            self.assertTrue(process_alive(child_pid))
                            guard.send_signal(signal.SIGTERM)
                        guard.wait(timeout=10)
                        log.seek(0)
                        self.assertEqual(guard.returncode, 0, log.read())
                        wait_for_process_exit(child_pid, timeout=5)
                    finally:
                        if guard.poll() is None:
                            guard.kill()
                        guard.wait(timeout=5)
                        if child_pid is not None and process_alive(child_pid):
                            os.killpg(child_pid, signal.SIGKILL)
                            wait_for_process_exit(child_pid, timeout=5)

    def test_sigterm_racing_revision_exec_survives_exec_as_shutdown_intent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            child_pid_file = root / "child.pid"
            trigger = root / "revision-changed"
            reexec_window = root / "reexec-window"
            child_code = (
                "import os,time; from pathlib import Path; "
                f"Path({str(child_pid_file)!r}).write_text(str(os.getpid())); "
                "time.sleep(30)"
            )
            code = textwrap.dedent(f"""
                import contextlib
                import sys
                import time
                from pathlib import Path
                from local_agent import entrypoint

                trigger = Path({str(trigger)!r})
                reexec_window = Path({str(reexec_window)!r})

                entrypoint.agent_remote_operator.poll_remote_operator = lambda *_a, **_k: None
                entrypoint.agent_operator.is_disabled = lambda: False
                entrypoint.load_repository_registry = lambda **_k: [object()]
                entrypoint.prepare_repositories = lambda _r: None
                entrypoint._recover_before_supervisor_start = lambda _r: None
                entrypoint.installation_pending = lambda _p: False
                entrypoint.supervisor_command = lambda _a: [sys.executable, '-c', {child_code!r}]

                @contextlib.contextmanager
                def transaction(_state_dir):
                    yield True

                entrypoint.installation_transaction = transaction
                entrypoint.agentd.self_revision = lambda: 'new' if trigger.exists() else 'old'

                original_stop = entrypoint.stop_supervisor
                def stop_with_race_window(proc):
                    if trigger.exists() and proc is not None and not reexec_window.exists():
                        reexec_window.touch()
                        time.sleep(1.0)
                    original_stop(proc)

                entrypoint.stop_supervisor = stop_with_race_window
                raise SystemExit(entrypoint.main())
            """)

            with (root / "guard.log").open("w+") as log:
                guard = subprocess.Popen(
                    [sys.executable, "-c", code],
                    cwd=REPO_ROOT,
                    env=dict(os.environ, HOME=tmp),
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                )
                child_pid = None
                try:
                    deadline = time.monotonic() + 10
                    while not child_pid_file.exists() and time.monotonic() < deadline:
                        time.sleep(0.05)
                    self.assertTrue(child_pid_file.exists())
                    child_pid = int(child_pid_file.read_text())
                    self.assertTrue(process_alive(child_pid))

                    trigger.touch()
                    deadline = time.monotonic() + 10
                    while not reexec_window.exists() and time.monotonic() < deadline:
                        time.sleep(0.05)
                    self.assertTrue(reexec_window.exists())

                    guard.send_signal(signal.SIGTERM)
                    guard.wait(timeout=10)
                    wait_for_process_exit(child_pid, timeout=5)

                    log.seek(0)
                    output = log.read()
                    self.assertEqual(guard.returncode, 0, output)
                    self.assertIn(
                        "inherited shutdown request after self re-exec; "
                        "exiting without supervisor start",
                        output,
                    )
                finally:
                    if guard.poll() is None:
                        guard.kill()
                    guard.wait(timeout=5)
                    if child_pid is not None and process_alive(child_pid):
                        os.killpg(child_pid, signal.SIGKILL)
                        wait_for_process_exit(child_pid, timeout=5)


if __name__ == "__main__":
    unittest.main()
