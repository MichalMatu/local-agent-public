from __future__ import annotations

import json
import shlex
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from local_agent.version import RELEASE_VERSION
from tests.test_multirepo_integration import (
    REPO_ROOT,
    create_repository_fixture,
    git,
    test_environment,
    write_registry,
)


def configure_parallel_barrier_task(
    item: dict[str, Path | str],
    *,
    own_marker: Path,
    peer_marker: Path,
) -> None:
    control = Path(item["control"])
    task_path = control / ".agent" / "tasks" / "shared-task-id.json"
    task = json.loads(task_path.read_text(encoding="utf-8"))
    own = shlex.quote(str(own_marker))
    peer = shlex.quote(str(peer_marker))
    task["resources"] = []
    task["memory_limit_mb"] = 256
    task["steps"] = [
        {
            "name": "parallel-barrier",
            "command": (
                f"touch {own}; "
                'i=0; while [ "$i" -lt 120 ]; do '
                f"[ -f {peer} ] && exit 0; "
                'i=$((i+1)); sleep 0.05; done; exit 7'
            ),
            "timeout": 15,
        }
    ]
    task_path.write_text(json.dumps(task, indent=2) + "\n", encoding="utf-8")
    git(["add", ".agent/tasks/shared-task-id.json"], cwd=control)
    git(["commit", "-m", f"Configure parallel barrier {item['id']}"], cwd=control)
    git(["push", "origin", "agent-control"], cwd=control)


def result_for(item: dict[str, Path | str]) -> dict:
    path = Path(item["control"]) / ".agent" / "results" / "shared-task-id.json"
    return json.loads(path.read_text(encoding="utf-8"))


class ParallelIntegrationTests(unittest.TestCase):
    def test_control_sync_holds_real_leases_without_reporting_quiescence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            item = create_repository_fixture(root, "control-watchdog")
            registry = write_registry(root, (item,))
            _home, env = test_environment(root)
            script = """
import os
import sys
from pathlib import Path
from unittest import mock
from local_agent import entrypoint
from local_agent.daemon import service as agentd
from local_agent.foundation.lease_recovery import repository_lease_paths, repository_leases_busy
from local_agent.repository.context import load_repository_registry
from local_agent.supervisor import orchestrator as parallel

repositories = load_repository_registry(path=Path(sys.argv[1]))
paths = repository_lease_paths(repositories, state_dir=agentd.STATE_DIR)
sync = parallel.supervisor_control.sync_control_quietly
leases = parallel.supervisor_control_leases
observations = []

def checked_leases(repositories):
    assert not entrypoint._quiescent_supervisor_status(os.getpid())
    return leases(repositories)

def checked_sync():
    assert repository_leases_busy(paths)
    assert not entrypoint._quiescent_supervisor_status(os.getpid())
    sync()
    assert repository_leases_busy(paths)
    assert not entrypoint._quiescent_supervisor_status(os.getpid())
    observations.append('synced')

def failed_sync():
    checked_sync()
    raise RuntimeError('injected failure after real Git sync')

parallel.publish_local_supervisor_status({}, max_workers=2)
for sync_function, expected in ((checked_sync, True), (failed_sync, False)):
    assert entrypoint._quiescent_supervisor_status(os.getpid())
    with mock.patch.object(parallel, 'supervisor_control_leases', checked_leases), \
         mock.patch.object(parallel.supervisor_control, 'sync_control_quietly', sync_function), \
         mock.patch.object(agentd, 'maybe_self_update'):
        result = parallel.service_control(
            repositories, registry_path=Path(sys.argv[1]), max_workers=2, once=True
        )
    assert result is expected
    assert not repository_leases_busy(paths)
    assert entrypoint._quiescent_supervisor_status(os.getpid())
assert observations == ['synced', 'synced']
worker_lease = parallel.serial_worker.repository_execution_lease
spawn = parallel.popen_registered
running = {}

def checked_worker_lease(repository):
    assert not entrypoint._quiescent_supervisor_status(os.getpid())
    return worker_lease(repository)

def checked_spawn(*args, **kwargs):
    assert repository_leases_busy(paths)
    assert not entrypoint._quiescent_supervisor_status(os.getpid())
    return spawn(*args, **kwargs)

with mock.patch.object(parallel.serial_worker, 'repository_execution_lease', checked_worker_lease), \
     mock.patch.object(parallel, 'popen_registered', checked_spawn):
    assert parallel.start_worker(
        repositories[0], registry_path=Path(sys.argv[1]), running=running, max_workers=2
    )
assert not entrypoint._quiescent_supervisor_status(os.getpid())
slot = running[repositories[0].repository_id]
try:
    slot.proc.wait(timeout=30)
    assert slot.proc.returncode == parallel.serial_worker.WORKER_PROCESSED
finally:
    if slot.proc.poll() is None:
        parallel.terminate_process_group(slot.proc, parallel.log)
    parallel.reap_workers(running, {})
parallel.publish_local_supervisor_status(running, max_workers=2)
assert not running
assert not repository_leases_busy(paths)
assert entrypoint._quiescent_supervisor_status(os.getpid())
print('control sync status and lease lifecycle verified')
"""
            result = subprocess.run(
                [sys.executable, "-c", script, str(registry)],
                cwd=REPO_ROOT,
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                timeout=60,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stdout)
            self.assertIn("control sync status and lease lifecycle verified", result.stdout)

    def test_two_parallel_safe_repositories_reach_shared_barrier(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = create_repository_fixture(root, "parallel-a")
            second = create_repository_fixture(root, "parallel-b")
            marker_a = root / "parallel-a.started"
            marker_b = root / "parallel-b.started"
            configure_parallel_barrier_task(
                first,
                own_marker=marker_a,
                peer_marker=marker_b,
            )
            configure_parallel_barrier_task(
                second,
                own_marker=marker_b,
                peer_marker=marker_a,
            )
            registry = write_registry(root, (first, second))
            home, env = test_environment(root)

            result = subprocess.run(
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
                timeout=90,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stdout)
            self.assertTrue(marker_a.exists(), result.stdout)
            self.assertTrue(marker_b.exists(), result.stdout)

            for item in (first, second):
                payload = result_for(item)
                self.assertEqual(payload["status"], "done", result.stdout)
                self.assertEqual(payload["daemon_version"], RELEASE_VERSION)
                claim_root = (
                    home
                    / "Library"
                    / "Application Support"
                    / "local-agent"
                    / "repositories"
                    / str(item["id"])
                    / "claims"
                )
                self.assertEqual(list(claim_root.glob("*.json")), [])
                self.assertEqual(
                    git(["status", "--porcelain"], cwd=Path(item["work"])),
                    "",
                )


if __name__ == "__main__":
    unittest.main()
