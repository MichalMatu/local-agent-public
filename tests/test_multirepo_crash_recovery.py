from __future__ import annotations

import json
import os
import shlex
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from tests.test_multirepo_integration import (
    REPO_ROOT,
    create_repository_fixture,
    git,
    test_environment,
    write_registry,
)


def wait_for_path(path: Path, *, timeout: float = 20.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            return
        time.sleep(0.05)
    raise AssertionError(f"timed out waiting for {path}")


def wait_for_worker_pid(status_path: Path, *, timeout: float = 20.0) -> int:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            payload = json.loads(status_path.read_text(encoding="utf-8"))
            pid = payload.get("pid")
            if payload.get("state") == "running" and isinstance(pid, int):
                return pid
        except (FileNotFoundError, json.JSONDecodeError):
            pass
        time.sleep(0.05)
    raise AssertionError(f"timed out waiting for running worker status at {status_path}")


def process_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    status = subprocess.run(
        ["ps", "-o", "stat=", "-p", str(pid)],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        timeout=5,
        check=False,
    )
    return status.returncode == 0 and not status.stdout.strip().startswith("Z")


def wait_for_process_exit(pid: int, *, timeout: float = 20.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not process_alive(pid):
            return
        time.sleep(0.05)
    detail = subprocess.run(
        ["ps", "-o", "pid=,ppid=,pgid=,stat=,command=", "-p", str(pid)],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=5,
        check=False,
    )
    raise AssertionError(
        f"timed out waiting for pid {pid} to exit: {detail.stdout.strip()}"
    )


def queue_blocking_task(
    item: dict[str, Path | str],
    *,
    started: Path,
    release: Path,
    finished: Path,
    executions: Path,
) -> None:
    script = (
        "from pathlib import Path; import os, time; "
        f"started=Path({str(started)!r}); "
        f"release=Path({str(release)!r}); "
        f"finished=Path({str(finished)!r}); "
        f"executions=Path({str(executions)!r}); "
        "executions.write_text((executions.read_text() if executions.exists() else '') "
        "+ 'run\\n', encoding='utf-8'); "
        "started.write_text(str(os.getpid()), encoding='utf-8'); "
        "exec(\"while not release.exists():\\n time.sleep(0.05)\"); "
        "finished.write_text('done\\n', encoding='utf-8')"
    )
    command = f"{shlex.quote(sys.executable)} -c {shlex.quote(script)}"
    control = Path(item["control"])
    task_path = control / ".agent" / "tasks" / "shared-task-id.json"
    task = json.loads(task_path.read_text(encoding="utf-8"))
    task["command_timeout"] = 60
    task["idle_timeout"] = 60
    task["task_timeout"] = 120
    task["steps"][0]["command"] = command
    task_path.write_text(json.dumps(task, indent=2) + "\n", encoding="utf-8")
    git(["add", ".agent/tasks/shared-task-id.json"], cwd=control)
    git(["commit", "-m", "Queue blocking crash-recovery task"], cwd=control)
    git(["push", "origin", "agent-control"], cwd=control)


class MultiRepositoryCrashRecoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.item = create_repository_fixture(self.root, "project-a")
        self.registry = write_registry(self.root, (self.item,))
        self.home, self.env = test_environment(self.root)
        self.started = self.root / "started"
        self.release = self.root / "release"
        self.finished = self.root / "finished"
        self.executions = self.root / "executions.log"
        queue_blocking_task(
            self.item,
            started=self.started,
            release=self.release,
            finished=self.finished,
            executions=self.executions,
        )
        self.supervisors: list[subprocess.Popen[str]] = []

    def tearDown(self) -> None:
        self.release.touch(exist_ok=True)
        for proc in self.supervisors:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=5)
            if proc.stdout is not None:
                proc.stdout.close()
        if self.started.exists():
            try:
                wait_for_process_exit(
                    int(self.started.read_text(encoding="utf-8")),
                    timeout=10,
                )
            except (AssertionError, ValueError):
                pass
        self.tmp.cleanup()

    def start_supervisor(self) -> subprocess.Popen[str]:
        proc = subprocess.Popen(
            [
                sys.executable,
                str(REPO_ROOT / "agent_multirepo.py"),
                "--registry",
                str(self.registry),
            ],
            cwd=REPO_ROOT,
            env=self.env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        self.supervisors.append(proc)
        return proc

    def run_once(self) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                sys.executable,
                str(REPO_ROOT / "agent_multirepo.py"),
                "--registry",
                str(self.registry),
                "--once",
            ],
            cwd=REPO_ROOT,
            env=self.env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=30,
            check=False,
        )

    def run_once_until_result(self, *, timeout: float = 20.0) -> subprocess.CompletedProcess[str]:
        deadline = time.monotonic() + timeout
        last: subprocess.CompletedProcess[str] | None = None
        while time.monotonic() < deadline:
            last = self.run_once()
            if last.returncode != 0 or self.result_path.exists():
                return last
            time.sleep(0.1)
        assert last is not None
        raise AssertionError(
            f"timed out waiting for recovery result; last output:\n{last.stdout}"
        )

    @property
    def status_path(self) -> Path:
        return (
            self.home
            / "Library"
            / "Application Support"
            / "local-agent"
            / "repositories"
            / "project-a"
            / "status.json"
        )

    @property
    def claim_dir(self) -> Path:
        return self.status_path.parent / "claims"

    @property
    def result_path(self) -> Path:
        return Path(self.item["control"]) / ".agent" / "results" / "shared-task-id.json"

    def assert_single_execution(self) -> None:
        self.assertEqual(self.executions.read_text(encoding="utf-8"), "run\n")

    def test_supervisor_sigkill_does_not_overlap_or_recover_live_worker(self) -> None:
        supervisor = self.start_supervisor()
        wait_for_path(self.started)
        worker_pid = wait_for_worker_pid(self.status_path)

        supervisor.kill()
        supervisor.wait(timeout=10)
        self.assertTrue(process_alive(worker_pid))

        blocked = self.run_once()
        self.assertEqual(blocked.returncode, 0, blocked.stdout)
        self.assertTrue(list(self.claim_dir.glob("*.json")))
        self.assertFalse(self.result_path.exists())
        self.assert_single_execution()

        self.release.touch()
        wait_for_path(self.finished)
        wait_for_path(self.result_path)
        wait_for_process_exit(worker_pid)
        result = json.loads(self.result_path.read_text(encoding="utf-8"))
        self.assertEqual(result["status"], "done")
        self.assert_single_execution()

    def test_worker_sigkill_keeps_lease_until_orphaned_command_exits(self) -> None:
        supervisor = self.start_supervisor()
        wait_for_path(self.started)
        worker_pid = wait_for_worker_pid(self.status_path)

        os.kill(worker_pid, signal.SIGKILL)
        wait_for_process_exit(worker_pid)
        supervisor.terminate()
        try:
            supervisor.wait(timeout=15)
        except subprocess.TimeoutExpired as exc:
            supervisor.kill()
            supervisor.wait(timeout=5)
            supervisor_output = (
                supervisor.stdout.read() if supervisor.stdout is not None else ""
            )
            self.fail(f"{exc}\nsupervisor output:\n{supervisor_output}")

        blocked = self.run_once()
        self.assertEqual(blocked.returncode, 0, blocked.stdout)
        self.assertTrue(list(self.claim_dir.glob("*.json")))
        self.assertFalse(self.result_path.exists())
        self.assert_single_execution()

        self.release.touch()
        wait_for_path(self.finished)
        command_pid = int(self.started.read_text(encoding="utf-8"))
        wait_for_process_exit(command_pid)
        recovered = self.run_once_until_result()
        self.assertEqual(recovered.returncode, 0, recovered.stdout)
        result = json.loads(self.result_path.read_text(encoding="utf-8"))
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["failure_reason"], "interrupted_previous_attempt")
        self.assert_single_execution()

    def test_supervisor_sigterm_terminates_worker_command_before_recovery(self) -> None:
        self.env["LOCAL_AGENT_STOP_GRACE_SECONDS"] = "1"
        supervisor = self.start_supervisor()
        wait_for_path(self.started)
        worker_pid = wait_for_worker_pid(self.status_path)
        command_pid = int(self.started.read_text(encoding="utf-8"))

        supervisor.terminate()
        supervisor.wait(timeout=20)
        supervisor_output = supervisor.stdout.read() if supervisor.stdout is not None else ""
        wait_for_process_exit(worker_pid)
        try:
            wait_for_process_exit(command_pid)
        except AssertionError as exc:
            self.fail(f"{exc}\nsupervisor output:\n{supervisor_output}")
        self.assertFalse(self.finished.exists())

        recovered = self.run_once_until_result()
        self.assertEqual(recovered.returncode, 0, recovered.stdout)
        result = json.loads(self.result_path.read_text(encoding="utf-8"))
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["failure_reason"], "interrupted_previous_attempt")
        self.assert_single_execution()


if __name__ == "__main__":
    unittest.main()
