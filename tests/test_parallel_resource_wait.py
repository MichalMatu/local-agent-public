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

from tests.test_multirepo_integration import (
    REPO_ROOT,
    create_repository_fixture,
    git,
    test_environment,
    write_registry,
)


RESOURCE = "board:test-s3"


def configure_named_resource_task(item: dict[str, Path | str]) -> None:
    control = Path(item["control"])
    task_path = control / ".agent" / "tasks" / "shared-task-id.json"
    task = json.loads(task_path.read_text(encoding="utf-8"))
    task["resources"] = [RESOURCE]
    task_path.write_text(json.dumps(task, indent=2) + "\n", encoding="utf-8")
    git(["add", ".agent/tasks/shared-task-id.json"], cwd=control)
    git(["commit", "-m", "Require named test resource"], cwd=control)
    git(["push", "origin", "agent-control"], cwd=control)


def wait_for_status(path: Path, state: str, timeout: float = 10.0) -> dict[str, object]:
    deadline = time.monotonic() + timeout
    last: dict[str, object] = {}
    while time.monotonic() < deadline:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            time.sleep(0.05)
            continue
        if isinstance(payload, dict):
            last = payload
            if payload.get("state") == state:
                return payload
        time.sleep(0.05)
    raise AssertionError(f"status {state!r} not observed; last={last!r}")


class ParallelResourceWaitIntegrationTests(unittest.TestCase):
    def test_waiting_resource_is_visible_and_task_runs_after_release(self) -> None:
        holder_code = textwrap.dedent(
            """
            import sys
            import time
            from pathlib import Path

            import local_agent.supervisor.worker as worker
            import local_agent.daemon.service as agentd

            state_dir = Path(sys.argv[1])
            release = Path(sys.argv[2])
            agentd.STATE_DIR = state_dir
            task = {"id": "holder", "resources": ["board:test-s3"]}
            with worker.machine_resource_lease(task):
                print("READY", flush=True)
                while not release.exists():
                    time.sleep(0.05)
            """
        )

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            item = create_repository_fixture(root, "resource-wait")
            configure_named_resource_task(item)
            registry = write_registry(root, (item,))
            home, env = test_environment(root)
            state_dir = home / "Library" / "Application Support" / "local-agent"
            release = root / "release-resource"
            repository_status = (
                state_dir
                / "repositories"
                / str(item["id"])
                / "status.json"
            )

            holder = subprocess.Popen(
                [
                    sys.executable,
                    "-c",
                    holder_code,
                    str(state_dir),
                    str(release),
                ],
                cwd=REPO_ROOT,
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
            supervisor: subprocess.Popen[str] | None = None
            try:
                assert holder.stdout is not None
                self.assertEqual(holder.stdout.readline().strip(), "READY")

                supervisor = subprocess.Popen(
                    [
                        sys.executable,
                        str(REPO_ROOT / "agent_parallel.py"),
                        "--registry",
                        str(registry),
                        "--max-workers",
                        "2",
                        "--once",
                    ],
                    cwd=REPO_ROOT,
                    env=env,
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                )

                waiting = wait_for_status(repository_status, "waiting_resource")
                self.assertEqual(waiting.get("pending_task_id"), "shared-task-id")
                self.assertEqual(waiting.get("blocked_resources"), [RESOURCE])
                self.assertTrue(waiting.get("retrying"))
                self.assertEqual(waiting.get("execution_variant"), "parallel")
                self.assertIsInstance(waiting.get("waiting_since"), str)

                release.touch()
                assert supervisor.stdout is not None
                output, _ = supervisor.communicate(timeout=30)
                self.assertEqual(supervisor.returncode, 0, output)

                result_path = (
                    Path(item["control"])
                    / ".agent"
                    / "results"
                    / "shared-task-id.json"
                )
                payload = json.loads(result_path.read_text(encoding="utf-8"))
                self.assertEqual(payload["status"], "done", output)
                final_status = wait_for_status(repository_status, "idle")
                self.assertEqual(final_status.get("last_task_id"), "shared-task-id")
            finally:
                release.touch(exist_ok=True)
                if supervisor is not None and supervisor.poll() is None:
                    with contextlib.suppress(ProcessLookupError):
                        os.killpg(supervisor.pid, signal.SIGKILL)
                    with contextlib.suppress(subprocess.TimeoutExpired):
                        supervisor.wait(timeout=5)
                if holder.poll() is None:
                    with contextlib.suppress(ProcessLookupError):
                        os.killpg(holder.pid, signal.SIGKILL)
                    with contextlib.suppress(subprocess.TimeoutExpired):
                        holder.wait(timeout=5)
                if holder.stdout is not None:
                    holder.stdout.close()


if __name__ == "__main__":
    unittest.main()
