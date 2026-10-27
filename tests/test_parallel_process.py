from __future__ import annotations

import contextlib
import json
import os
import signal
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path
from unittest import mock

from local_agent.foundation.process import (
    LEASE_FDS_ENV, LEASE_KEYS_DIGEST_ENV, RESOURCE_LEASE_FDS_ENV,
)

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_env() -> dict[str, str]:
    env = os.environ.copy()
    # These disposable subprocesses acquire their own leases. Popen closes
    # ambient service FDs, so their numbers must not leak into fixture children.
    for name in (LEASE_FDS_ENV, LEASE_KEYS_DIGEST_ENV, RESOURCE_LEASE_FDS_ENV):
        env.pop(name, None)
    env["PYTHONPATH"] = str(REPO_ROOT)
    return env


def run_contender(state_dir: Path, task: dict[str, object]) -> subprocess.CompletedProcess[str]:
    code = textwrap.dedent(
        """
        import json
        import sys
        from pathlib import Path

        import local_agent.supervisor.worker as worker
        import local_agent.daemon.service as agentd

        agentd.STATE_DIR = Path(sys.argv[1])
        task = json.loads(sys.argv[2])
        try:
            with worker.machine_resource_lease(task):
                print("ACQUIRED", flush=True)
        except worker.MachineResourceBusy as exc:
            print(f"BUSY:{exc.resource}", flush=True)
            raise SystemExit(23)
        """
    )
    return subprocess.run(
        [sys.executable, "-c", code, str(state_dir), json.dumps(task)],
        cwd=REPO_ROOT,
        env=test_env(),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=10,
        check=False,
    )


class ParallelResourceProcessTests(unittest.TestCase):
    def test_fixture_environment_isolates_parent_lease_evidence(self) -> None:
        with mock.patch.dict(os.environ, {
            LEASE_FDS_ENV: "200", LEASE_KEYS_DIGEST_ENV: "parent",
            RESOURCE_LEASE_FDS_ENV: "201", "FIXTURE_UNRELATED": "preserved",
        }):
            env = test_env()
            for name in (LEASE_FDS_ENV, LEASE_KEYS_DIGEST_ENV, RESOURCE_LEASE_FDS_ENV):
                self.assertNotIn(name, env)
                self.assertIn(name, os.environ)
            self.assertEqual(env["FIXTURE_UNRELATED"], "preserved")

    def test_distinct_named_resources_run_concurrently_but_same_or_machine_conflicts(self) -> None:
        holder_code = textwrap.dedent(
            """
            import sys
            import time
            from pathlib import Path

            import local_agent.supervisor.worker as worker
            import local_agent.daemon.service as agentd

            agentd.STATE_DIR = Path(sys.argv[1])
            task = {"id": "holder", "resources": ["browser:chrome"]}
            with worker.machine_resource_lease(task):
                print("READY", flush=True)
                time.sleep(30)
            """
        )
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / "state"
            holder = subprocess.Popen(
                [sys.executable, "-c", holder_code, str(state_dir)],
                cwd=REPO_ROOT,
                env=test_env(),
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
            try:
                assert holder.stdout is not None
                self.assertEqual(holder.stdout.readline().strip(), "READY")

                different = run_contender(
                    state_dir,
                    {"id": "different", "resources": ["usb:esp32"]},
                )
                self.assertEqual(different.returncode, 0, different.stdout)
                self.assertIn("ACQUIRED", different.stdout)

                same = run_contender(
                    state_dir,
                    {"id": "same", "resources": ["browser:chrome"]},
                )
                self.assertEqual(same.returncode, 23, same.stdout)
                self.assertIn("BUSY:browser:chrome", same.stdout)

                machine = run_contender(
                    state_dir,
                    {"id": "machine", "resources": ["machine"]},
                )
                self.assertEqual(machine.returncode, 23, machine.stdout)
                self.assertIn("BUSY:machine", machine.stdout)
            finally:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(holder.pid, signal.SIGKILL)
                with contextlib.suppress(subprocess.TimeoutExpired):
                    holder.wait(timeout=5)
                if holder.stdout is not None:
                    holder.stdout.close()

    def test_full_machine_lock_excludes_software_only_holder(self) -> None:
        holder_code = textwrap.dedent(
            """
            import sys
            import time
            from pathlib import Path

            import local_agent.supervisor.worker as worker
            import local_agent.daemon.service as agentd

            agentd.STATE_DIR = Path(sys.argv[1])
            task = {"id": "holder", "resources": [], "memory_limit_mb": 4096}
            with worker.machine_resource_lease(task):
                print("READY", flush=True)
                time.sleep(30)
            """
        )
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / "state"
            holder = subprocess.Popen(
                [sys.executable, "-c", holder_code, str(state_dir)],
                cwd=REPO_ROOT,
                env=test_env(),
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
            try:
                assert holder.stdout is not None
                self.assertEqual(holder.stdout.readline().strip(), "READY")
                contender = run_contender(
                    state_dir,
                    {"id": "exclusive", "resources": ["machine"]},
                )
                self.assertEqual(contender.returncode, 23, contender.stdout)
                self.assertIn("BUSY:machine", contender.stdout)
            finally:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(holder.pid, signal.SIGKILL)
                with contextlib.suppress(subprocess.TimeoutExpired):
                    holder.wait(timeout=5)
                if holder.stdout is not None:
                    holder.stdout.close()

    def test_named_resource_lock_survives_worker_exit_via_descendant_fd(self) -> None:
        holder_code = textwrap.dedent(
            """
            import os
            import subprocess
            import sys
            from pathlib import Path

            import local_agent.supervisor.worker as worker
            import local_agent.daemon.service as agentd
            from local_agent.foundation.process import popen_registered

            agentd.STATE_DIR = Path(sys.argv[1])
            pid_path = Path(sys.argv[2])
            task = {
                "id": "holder",
                "resources": ["usb"],
                "memory_limit_mb": 256,
            }
            with worker.machine_resource_lease(task):
                descendant = popen_registered(
                    [sys.executable, "-c", "import time; time.sleep(30)"],
                    env=os.environ.copy(),
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    start_new_session=True,
                )
                pid_path.write_text(str(descendant.pid), encoding="utf-8")
                print("READY", flush=True)
                os._exit(0)
            """
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state_dir = root / "state"
            pid_path = root / "descendant.pid"
            holder = subprocess.Popen(
                [
                    sys.executable,
                    "-c",
                    holder_code,
                    str(state_dir),
                    str(pid_path),
                ],
                cwd=REPO_ROOT,
                env=test_env(),
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
            )
            descendant_pid: int | None = None
            try:
                output, _ = holder.communicate(timeout=5)
                self.assertEqual(holder.returncode, 0, output)
                self.assertEqual(output.strip(), "READY", output)
                descendant_pid = int(pid_path.read_text(encoding="utf-8"))

                task = {
                    "id": "contender",
                    "resources": ["usb"],
                    "memory_limit_mb": 256,
                }
                contender = run_contender(state_dir, task)
                self.assertEqual(contender.returncode, 23, contender.stdout)
                self.assertIn("BUSY:usb", contender.stdout)

                os.killpg(descendant_pid, signal.SIGKILL)
                deadline = time.monotonic() + 5.0
                while True:
                    contender = run_contender(state_dir, task)
                    if contender.returncode == 0:
                        break
                    if time.monotonic() >= deadline:
                        self.fail(
                            "resource lock remained busy after descendant exit:\n"
                            + contender.stdout
                        )
                    time.sleep(0.05)
                self.assertIn("ACQUIRED", contender.stdout)
            finally:
                if descendant_pid is not None:
                    with contextlib.suppress(ProcessLookupError):
                        os.killpg(descendant_pid, signal.SIGKILL)
                if holder.poll() is None:
                    holder.kill()
                    with contextlib.suppress(subprocess.TimeoutExpired):
                        holder.wait(timeout=5)
                if holder.stdout is not None:
                    holder.stdout.close()


if __name__ == "__main__":
    unittest.main()
