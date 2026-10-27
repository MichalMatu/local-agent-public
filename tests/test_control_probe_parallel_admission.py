from __future__ import annotations

import json
import shlex
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


def wait_for_path(path: Path, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            return True
        time.sleep(0.02)
    return path.exists()


def configure_control_hold_task(
    item: dict[str, Path | str],
    *,
    started_marker: Path,
    release_marker: Path,
) -> None:
    control = Path(item["control"])
    task_path = control / ".agent" / "tasks" / "shared-task-id.json"
    task = json.loads(task_path.read_text(encoding="utf-8"))
    started = shlex.quote(str(started_marker))
    release = shlex.quote(str(release_marker))
    task["resources"] = []
    task["memory_limit_mb"] = 256
    # Hold long enough that hosted macOS startup latency cannot race the assertion.
    task["command_timeout"] = 30
    task["idle_timeout"] = 20
    task["task_timeout"] = 120
    task["steps"] = [
        {
            "name": "hold-control-repository",
            "command": (
                f"touch {started}; "
                'i=0; while [ "$i" -lt 3000 ]; do '
                f"[ -f {release} ] && exit 0; "
                'i=$((i+1)); sleep 0.01; done; exit 7'
            ),
            "timeout": 30,
        }
    ]
    task_path.write_text(json.dumps(task, indent=2) + "\n", encoding="utf-8")
    git(["add", ".agent/tasks/shared-task-id.json"], cwd=control)
    git(["commit", "-m", "Configure long-running control repository task"], cwd=control)
    git(["push", "origin", "agent-control"], cwd=control)


def remove_initial_task(item: dict[str, Path | str]) -> None:
    control = Path(item["control"])
    git(["rm", ".agent/tasks/shared-task-id.json"], cwd=control)
    git(["commit", "-m", "Remove initial task for late-admission test"], cwd=control)
    git(["push", "origin", "agent-control"], cwd=control)


def queue_late_task(
    root: Path,
    item: dict[str, Path | str],
    *,
    marker: Path,
) -> None:
    control = Path(item["control"])
    remote = git(["remote", "get-url", "origin"], cwd=control).strip()
    queue_checkout = root / "late-task-queue"
    git(["clone", "--branch", "agent-control", remote, str(queue_checkout)])
    git(["config", "user.name", "local-agent-tests"], cwd=queue_checkout)
    git(["config", "user.email", "local-agent-tests@example.invalid"], cwd=queue_checkout)

    task_id = "late-parallel-task"
    task = {
        "id": task_id,
        "agent_binding": str(item["agent_binding"]),
        "mode": "commands",
        "work_branch": "main",
        "allow_write": False,
        "resources": [],
        "command_timeout": 15,
        "idle_timeout": 10,
        "task_timeout": 120,
        "memory_limit_mb": 256,
        "steps": [
            {
                "name": "late-parallel-admission",
                "command": f"touch {shlex.quote(str(marker))}",
                "timeout": 15,
            }
        ],
    }
    task_path = queue_checkout / ".agent" / "tasks" / f"{task_id}.json"
    task_path.write_text(json.dumps(task, indent=2) + "\n", encoding="utf-8")
    git(["add", str(task_path.relative_to(queue_checkout))], cwd=queue_checkout)
    git(["commit", "-m", "Queue late parallel task"], cwd=queue_checkout)
    git(["push", "origin", "agent-control"], cwd=queue_checkout)


class ControlProbeParallelAdmissionIntegrationTests(unittest.TestCase):
    def test_running_control_repository_does_not_drain_unrelated_admission(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            control_repo = create_repository_fixture(root, "control-running")
            late_repo = create_repository_fixture(root, "late-repository")
            control_started = root / "control.started"
            control_release = root / "control.release"
            late_started = root / "late.started"

            configure_control_hold_task(
                control_repo,
                started_marker=control_started,
                release_marker=control_release,
            )
            remove_initial_task(late_repo)
            registry = write_registry(root, (control_repo, late_repo))
            _home, env = test_environment(root)

            script = r"""
import sys
from local_agent.supervisor import orchestrator as parallel

parallel.REAP_INTERVAL_SECONDS = 0.01
parallel.REPOSITORY_RETRY_SECONDS = 0.01
parallel.ERROR_RETRY_SECONDS = 0.01
parallel.supervisor_policy.POLL_SECONDS = 0.05
parallel.supervisor_policy.HOT_POLL_SECONDS = 0.05
parallel.supervisor_policy.WARM_POLL_SECONDS = 0.05
parallel.supervisor_policy.SUPERVISOR_CONTROL_POLL_SECONDS = 0.05
parallel.scheduling.CONTROL_DEFER_RETRY_BASE_SECONDS = 0.05
parallel.scheduling.CONTROL_DEFER_RETRY_MAX_SECONDS = 0.05
sys.argv = [
    "agent_parallel.py",
    "--registry",
    sys.argv[1],
    "--max-workers",
    "2",
]
raise SystemExit(parallel.main())
"""
            proc = subprocess.Popen(
                [sys.executable, "-c", script, str(registry)],
                cwd=REPO_ROOT,
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
            )
            output = ""
            try:
                if not wait_for_path(control_started, 10):
                    proc.terminate()
                    output, _ = proc.communicate(timeout=10)
                    self.fail(f"control repository task did not start:\n{output}")

                # Six true lease-busy outcomes fit comfortably in this window after
                # shortening only test polling/backoff constants.
                time.sleep(0.6)
                queue_late_task(root, late_repo, marker=late_started)

                # Admission is the contract; command-start latency is not a 4 s SLA.
                if not wait_for_path(late_started, 10):
                    proc.terminate()
                    output, _ = proc.communicate(timeout=10)
                    self.fail(
                        "late unrelated task was not admitted while the control repository "
                        f"was still running:\n{output}"
                    )

                self.assertFalse(
                    control_release.exists(),
                    "control task was released before parallel admission was proven",
                )
                control_release.touch()

                first_result = (
                    Path(control_repo["control"])
                    / ".agent"
                    / "results"
                    / "shared-task-id.json"
                )
                late_result = (
                    Path(late_repo["control"])
                    / ".agent"
                    / "results"
                    / "late-parallel-task.json"
                )
                self.assertTrue(wait_for_path(first_result, 8))
                self.assertTrue(wait_for_path(late_result, 8))
            finally:
                control_release.touch(exist_ok=True)
                if proc.poll() is None:
                    proc.terminate()
                try:
                    output, _ = proc.communicate(timeout=10)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    output, _ = proc.communicate(timeout=10)

            self.assertIn(
                "pausing new control-repository admission",
                output,
                "regression must cross the repeated known-worker lease-busy threshold",
            )
            self.assertIn("consecutive_lease_busy=6", output)
            self.assertNotIn(
                "draining active workers consecutive_lease_busy=",
                output,
            )


if __name__ == "__main__":
    unittest.main()
