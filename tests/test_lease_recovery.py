from __future__ import annotations

import fcntl
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import local_agent.foundation.process as agent_process
from local_agent import entrypoint
from local_agent.foundation.lease_recovery import (
    recover_orphaned_repository_leases,
    repository_leases_busy,
)
from local_agent.foundation.process import (
    LEASE_FDS_ENV,
    LEASE_KEYS_DIGEST_ENV,
    RESOURCE_LEASE_FDS_ENV,
    acquire_execution_leases,
    popen_registered,
    unregister_process,
)


class LeaseRecoveryTests(unittest.TestCase):
    def tearDown(self) -> None:
        agent_process.reset_process_lifecycle_for_tests()

    def test_busy_probe_does_not_require_lease_metadata_rewrite(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            lock_dir = Path(tmp) / "locks"
            leases = acquire_execution_leases(lock_dir, ("repository:one",))
            try:
                self.assertTrue(repository_leases_busy(leases.paths))
            finally:
                leases.close()
            self.assertFalse(repository_leases_busy(leases.paths))

    def test_orphan_recovery_releases_inherited_resource_lock(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            lock_dir = root / "repository-locks"
            leases = acquire_execution_leases(lock_dir, ("repository:one",))
            resource_path = root / "machine.lock"
            resource = resource_path.open("a+", encoding="utf-8")
            fcntl.flock(resource.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            env = os.environ.copy()
            env.update(leases.environment())
            env[RESOURCE_LEASE_FDS_ENV] = str(resource.fileno())
            holder = popen_registered(
                [
                    sys.executable,
                    "-c",
                    "import time; print('holder-ready', flush=True); time.sleep(60)",
                ],
                cwd=root,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                start_new_session=True,
            )
            probe = resource_path.open("a+", encoding="utf-8")
            try:
                assert holder.stdout is not None
                self.assertEqual(holder.stdout.readline().strip(), "holder-ready")
                paths = leases.paths
                leases.close()
                resource.close()
                self.assertTrue(repository_leases_busy(paths))
                with self.assertRaises(BlockingIOError):
                    fcntl.flock(probe.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

                recovered = recover_orphaned_repository_leases(
                    paths,
                    log=lambda _message: None,
                    grace_seconds=0.1,
                )
                self.assertEqual(recovered, (holder.pid,))
                holder.wait(timeout=5)
                self.assertNotEqual(holder.returncode, 0)
                self.assertFalse(repository_leases_busy(paths))

                fcntl.flock(probe.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                fcntl.flock(probe.fileno(), fcntl.LOCK_UN)
            finally:
                if holder.poll() is None:
                    holder.kill()
                    holder.wait(timeout=5)
                if holder.stdout is not None:
                    holder.stdout.close()
                unregister_process(holder)
                probe.close()
                if not resource.closed:
                    resource.close()
                leases.close()

    def test_orphan_recovery_kills_only_actual_lock_holder(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            lock_dir = root / "locks"
            leases = acquire_execution_leases(lock_dir, ("repository:one",))
            env = os.environ.copy()
            env.update(leases.environment())
            holder_code = (
                "import signal,time; "
                "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
                "print('holder-ready', flush=True); "
                "time.sleep(60)"
            )
            holder = popen_registered(
                [sys.executable, "-c", holder_code],
                cwd=root,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                start_new_session=True,
            )
            observer: subprocess.Popen[str] | None = None
            try:
                assert holder.stdout is not None
                self.assertEqual(holder.stdout.readline().strip(), "holder-ready")
                paths = leases.paths
                leases.close()
                self.assertTrue(repository_leases_busy(paths))

                observer_env = os.environ.copy()
                for name in (LEASE_FDS_ENV, LEASE_KEYS_DIGEST_ENV, RESOURCE_LEASE_FDS_ENV):
                    observer_env.pop(name, None)
                observer_code = (
                    "import sys,time; "
                    "handle=open(sys.argv[1], 'r'); "
                    "print('observer-ready', flush=True); "
                    "time.sleep(60)"
                )
                observer = popen_registered(
                    [sys.executable, "-c", observer_code, str(paths[0])],
                    cwd=root,
                    env=observer_env,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    start_new_session=True,
                )
                assert observer.stdout is not None
                self.assertEqual(observer.stdout.readline().strip(), "observer-ready")

                recovered = recover_orphaned_repository_leases(
                    paths,
                    log=lambda _message: None,
                    grace_seconds=0.1,
                )
                self.assertEqual(recovered, (holder.pid,))
                holder.wait(timeout=5)
                self.assertNotEqual(holder.returncode, 0)
                self.assertIsNone(observer.poll())
                self.assertFalse(repository_leases_busy(paths))

                reacquired = acquire_execution_leases(lock_dir, ("repository:one",))
                reacquired.close()
            finally:
                if holder.poll() is None:
                    holder.kill()
                    holder.wait(timeout=5)
                if holder.stdout is not None:
                    holder.stdout.close()
                unregister_process(holder)
                if observer is not None:
                    if observer.poll() is None:
                        observer.kill()
                        observer.wait(timeout=5)
                    if observer.stdout is not None:
                        observer.stdout.close()
                    unregister_process(observer)


class EntrypointLeaseWatchdogTests(unittest.TestCase):
    def test_lease_observation_rejects_status_changes_during_probe(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            status = Path(tmp) / "status.json"
            initial = {
                "state": "idle", "supervisor_pid": 1234,
                "active_repository_ids": [], "updated_at": "first",
            }
            for state in ("running", "idle"):
                for busy in (True, False):
                    with self.subTest(state=state, busy=busy):
                        status.write_text(json.dumps(initial), encoding="utf-8")

                        def changed_probe(_paths):
                            status.write_text(json.dumps({
                                **initial, "state": state, "updated_at": "next",
                            }), encoding="utf-8")
                            return busy

                        with mock.patch.object(entrypoint.agentd, "LOCAL_STATUS_PATH", status), \
                             mock.patch.object(entrypoint, "repository_leases_busy", changed_probe):
                            self.assertIsNone(entrypoint._quiescent_lease_observation(1234, ()))

    def test_stable_idle_snapshot_accepts_busy_and_free_observations(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            status = Path(tmp) / "status.json"
            status.write_text(json.dumps({
                "state": "idle", "supervisor_pid": 1234, "active_repository_ids": [],
            }), encoding="utf-8")
            for busy in (True, False):
                with mock.patch.object(entrypoint.agentd, "LOCAL_STATUS_PATH", status), \
                     mock.patch.object(entrypoint, "repository_leases_busy", return_value=busy):
                    self.assertIs(entrypoint._quiescent_lease_observation(1234, ()), busy)
            with mock.patch.object(entrypoint.agentd, "LOCAL_STATUS_PATH", status), \
                 mock.patch.object(entrypoint, "repository_leases_busy") as probe:
                self.assertIsNone(entrypoint._quiescent_lease_observation(5678, ()))
                probe.assert_not_called()

    def test_quiescent_status_must_belong_to_current_supervisor(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            status = Path(tmp) / "status.json"
            status.write_text(
                json.dumps(
                    {
                        "state": "idle",
                        "supervisor_pid": 1234,
                        "active_repository_ids": [],
                    }
                ),
                encoding="utf-8",
            )
            with mock.patch.object(entrypoint.agentd, "LOCAL_STATUS_PATH", status):
                self.assertTrue(entrypoint._quiescent_supervisor_status(1234))
                self.assertFalse(entrypoint._quiescent_supervisor_status(5678))

    def test_running_repository_is_not_quiescent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            status = Path(tmp) / "status.json"
            status.write_text(
                json.dumps(
                    {
                        "state": "running",
                        "supervisor_pid": 1234,
                        "active_repository_ids": ["example-tracker"],
                    }
                ),
                encoding="utf-8",
            )
            with mock.patch.object(entrypoint.agentd, "LOCAL_STATUS_PATH", status):
                self.assertFalse(entrypoint._quiescent_supervisor_status(1234))

    def test_global_control_status_is_not_quiescent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            status = Path(tmp) / "status.json"
            status.write_text(
                json.dumps(
                    {
                        "state": "idle",
                        "supervisor_pid": 1234,
                        "active_repository_ids": [],
                        "supervisor_control_repository": "litegraph",
                    }
                ),
                encoding="utf-8",
            )
            with mock.patch.object(entrypoint.agentd, "LOCAL_STATUS_PATH", status):
                self.assertFalse(entrypoint._quiescent_supervisor_status(1234))

    def test_busy_watch_survives_transient_scheduler_activity(self) -> None:
        started = entrypoint._updated_quiescent_lease_watch(
            None,
            supervisor_quiescent=True,
            leases_busy=True,
            now=10.0,
        )
        self.assertEqual(started, 10.0)

        preserved = entrypoint._updated_quiescent_lease_watch(
            started,
            supervisor_quiescent=False,
            leases_busy=False,
            now=20.0,
        )
        self.assertEqual(preserved, 10.0)

        continued = entrypoint._updated_quiescent_lease_watch(
            preserved,
            supervisor_quiescent=True,
            leases_busy=True,
            now=41.0,
        )
        self.assertEqual(continued, 10.0)
        assert continued is not None
        self.assertGreaterEqual(
            41.0 - continued,
            entrypoint.QUIESCENT_LEASE_STALL_SECONDS,
        )

    def test_verified_quiescent_free_observation_clears_busy_watch(self) -> None:
        cleared = entrypoint._updated_quiescent_lease_watch(
            10.0,
            supervisor_quiescent=True,
            leases_busy=False,
            now=20.0,
        )
        self.assertIsNone(cleared)

    def test_orphan_recovery_guard_refuses_live_daemon(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            lock_path = Path(tmp) / "agentd.lock"
            handle = lock_path.open("a+", encoding="utf-8")
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                with mock.patch.object(entrypoint.agentd, "DAEMON_LOCK_PATH", lock_path):
                    with self.assertRaisesRegex(RuntimeError, "refusing orphan recovery"):
                        with entrypoint._orphan_recovery_guard():
                            self.fail("guard must not enter while another daemon owns the lock")
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                handle.close()


if __name__ == "__main__":
    unittest.main()
