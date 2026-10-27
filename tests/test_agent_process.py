from __future__ import annotations

import os
import signal
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import local_agent.foundation.process as agent_process
from local_agent.foundation.process import (
    BoundedTextBuffer,
    ExecutionLeaseBusy,
    acquire_execution_leases,
    defer_termination,
    process_group_for,
    process_shutdown_requested,
    spawn_shell,
    termination_critical_section,
    terminate_active_processes,
    terminate_process_group,
    unregister_process,
)


class BoundedTextBufferTests(unittest.TestCase):
    def test_buffer_keeps_exact_tail_across_chunks(self) -> None:
        buffer = BoundedTextBuffer(8)
        buffer.append("abc")
        buffer.append("defgh")
        buffer.append("ij")
        self.assertEqual(buffer.text(), "cdefghij")
        self.assertEqual(len(buffer), 8)

    def test_single_large_chunk_is_strictly_bounded(self) -> None:
        buffer = BoundedTextBuffer(5)
        buffer.append("0123456789")
        self.assertEqual(buffer.text(), "56789")
        self.assertEqual(len(buffer), 5)


class ProcessGroupTests(unittest.TestCase):
    def tearDown(self) -> None:
        agent_process.reset_process_lifecycle_for_tests()

    def test_stored_process_group_survives_parent_exit(self) -> None:
        proc = mock.Mock(spec=subprocess.Popen)
        proc.pid = 123
        proc.poll.return_value = 0
        setattr(proc, "_local_agent_process_group", 456)
        self.assertEqual(process_group_for(proc), 456)

    def test_terminate_escalates_when_group_remains_alive(self) -> None:
        proc = mock.Mock(spec=subprocess.Popen)
        proc.pid = 123
        setattr(proc, "_local_agent_process_group", 456)
        log = mock.Mock()
        with mock.patch("local_agent.foundation.process.os.getpgrp", return_value=999), mock.patch(
            "local_agent.foundation.process.os.killpg"
        ) as killpg, mock.patch(
            "local_agent.foundation.process.process_group_alive", side_effect=[True, True, True]
        ), mock.patch("local_agent.foundation.process.time.monotonic", side_effect=[0.0, 0.0, 1.0]), mock.patch(
            "local_agent.foundation.process.time.sleep"
        ):
            terminate_process_group(proc, log, grace_seconds=0.5)

        self.assertEqual(killpg.call_args_list[0].args[0], 456)
        self.assertEqual(killpg.call_args_list[-1].args[0], 456)
        self.assertEqual(killpg.call_count, 2)

    def test_terminate_active_processes_stops_real_registered_group(self) -> None:
        proc = spawn_shell("sleep 30", cwd=Path.cwd(), env=os.environ)
        try:
            self.assertEqual(
                terminate_active_processes(
                    lambda _message: None,
                    grace_seconds=0.2,
                ),
                1,
            )
            proc.wait(timeout=5)
            self.assertIsNotNone(proc.returncode)
        finally:
            if proc.poll() is None:
                terminate_process_group(proc, lambda _message: None, grace_seconds=0.1)
            if proc.stdout is not None:
                proc.stdout.close()
            unregister_process(proc)

    def test_terminate_active_processes_does_not_commit_process_shutdown(self) -> None:
        self.assertEqual(
            terminate_active_processes(lambda _message: None, grace_seconds=0.0),
            0,
        )
        self.assertFalse(process_shutdown_requested())

    def test_signal_request_commits_shutdown_before_immediate_delivery(self) -> None:
        self.assertFalse(defer_termination(signal.SIGUSR1))
        self.assertTrue(process_shutdown_requested())
        with self.assertRaisesRegex(RuntimeError, "process spawn rejected during shutdown"):
            agent_process.popen_registered(["ignored"])

    def test_terminate_active_processes_tolerates_unreachable_exited_group(self) -> None:
        proc = mock.Mock(spec=subprocess.Popen)
        proc.pid = 12345
        proc.poll.return_value = -signal.SIGKILL
        with mock.patch("local_agent.foundation.process.subprocess.Popen", return_value=proc):
            agent_process.popen_registered(["ignored"], start_new_session=True)
        with mock.patch("local_agent.foundation.process.os.getpgrp", return_value=99999), mock.patch(
            "local_agent.foundation.process.os.killpg",
            side_effect=PermissionError(1, "operation not permitted"),
        ):
            self.assertEqual(
                terminate_active_processes(lambda _message: None, grace_seconds=0.0),
                1,
            )

    def test_signal_during_spawn_is_redelivered_after_registration(self) -> None:
        proc = mock.Mock(spec=subprocess.Popen)
        proc.pid = 12345
        proc.poll.return_value = 0
        deliveries: list[str] = []
        previous = signal.getsignal(signal.SIGUSR1)

        def handler(signum: int, _frame: object) -> None:
            if defer_termination(signum):
                deliveries.append("deferred")
                return
            deliveries.append("delivered")
            raise SystemExit(128 + signum)

        def spawn(*_args: object, **_kwargs: object) -> mock.Mock:
            os.kill(os.getpid(), signal.SIGUSR1)
            return proc

        signal.signal(signal.SIGUSR1, handler)
        try:
            with mock.patch("local_agent.foundation.process.subprocess.Popen", side_effect=spawn):
                with self.assertRaisesRegex(SystemExit, str(128 + signal.SIGUSR1)):
                    agent_process.popen_registered(
                        ["ignored"],
                        start_new_session=True,
                    )
        finally:
            signal.signal(signal.SIGUSR1, previous)

        self.assertEqual(deliveries, ["deferred", "delivered"])
        self.assertEqual(process_group_for(proc), proc.pid)

    def test_signal_is_deferred_across_critical_subprocess_transaction(self) -> None:
        proc = mock.Mock(spec=subprocess.Popen)
        proc.pid = 12345
        proc.poll.return_value = 0
        deliveries: list[str] = []
        previous = signal.getsignal(signal.SIGUSR1)

        def handler(signum: int, _frame: object) -> None:
            if defer_termination(signum):
                deliveries.append("deferred")
                return
            deliveries.append("delivered")
            raise SystemExit(128 + signum)

        signal.signal(signal.SIGUSR1, handler)
        try:
            with mock.patch("local_agent.foundation.process.subprocess.Popen", return_value=proc):
                with self.assertRaisesRegex(SystemExit, str(128 + signal.SIGUSR1)):
                    with termination_critical_section():
                        os.kill(os.getpid(), signal.SIGUSR1)
                        spawned = agent_process.popen_registered(
                            ["ignored"],
                            start_new_session=True,
                        )
                        unregister_process(spawned)
        finally:
            signal.signal(signal.SIGUSR1, previous)

        self.assertEqual(deliveries, ["deferred", "delivered"])


class ExecutionLeaseTests(unittest.TestCase):
    def tearDown(self) -> None:
        agent_process.reset_process_lifecycle_for_tests()

    def test_second_open_description_cannot_acquire_held_lease(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            lock_dir = Path(tmp) / "locks"
            first = acquire_execution_leases(lock_dir, ("repository:one",))
            try:
                with self.assertRaises(ExecutionLeaseBusy):
                    acquire_execution_leases(lock_dir, ("repository:one",))
            finally:
                first.close()
            second = acquire_execution_leases(lock_dir, ("repository:one",))
            second.close()

    def test_failed_lease_metadata_write_releases_already_acquired_lock(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            lock_dir = Path(tmp) / "locks"
            with mock.patch(
                "local_agent.foundation.process.os.fsync",
                side_effect=OSError("fsync failed"),
            ):
                with self.assertRaisesRegex(OSError, "fsync failed"):
                    acquire_execution_leases(lock_dir, ("repository:one",))
            acquired = acquire_execution_leases(lock_dir, ("repository:one",))
            acquired.close()

    def test_process_spawn_passes_repository_and_resource_lease_channels(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_fd = os.open(Path(tmp) / "repo.lock", os.O_RDWR | os.O_CREAT, 0o600)
            resource_fd = os.open(
                Path(tmp) / "resource.lock", os.O_RDWR | os.O_CREAT, 0o600
            )
            proc = mock.Mock(spec=subprocess.Popen)
            proc.pid = 12345
            proc.poll.return_value = 0
            env = dict(os.environ)
            env[agent_process.LEASE_FDS_ENV] = str(repo_fd)
            env[agent_process.RESOURCE_LEASE_FDS_ENV] = str(resource_fd)
            try:
                with mock.patch(
                    "local_agent.foundation.process.subprocess.Popen", return_value=proc
                ) as popen:
                    spawned = agent_process.popen_registered(["ignored"], env=env)
                self.assertEqual(
                    popen.call_args.kwargs["pass_fds"],
                    tuple(sorted((repo_fd, resource_fd))),
                )
                unregister_process(spawned)
            finally:
                os.close(repo_fd)
                os.close(resource_fd)

    def test_inherited_lease_survives_owner_close_until_child_exits(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            lock_dir = Path(tmp) / "locks"
            leases = acquire_execution_leases(lock_dir, ("repository:one",))
            child_env = dict(os.environ)
            child_env.update(leases.environment())
            proc = spawn_shell("sleep 30", cwd=Path(tmp), env=child_env)
            leases.close()
            try:
                with self.assertRaises(ExecutionLeaseBusy):
                    acquire_execution_leases(lock_dir, ("repository:one",))
                terminate_process_group(proc, lambda _message: None, grace_seconds=0.1)
                proc.wait(timeout=5)
                deadline = time.monotonic() + 5
                while True:
                    try:
                        acquired = acquire_execution_leases(
                            lock_dir,
                            ("repository:one",),
                        )
                    except ExecutionLeaseBusy:
                        if time.monotonic() >= deadline:
                            self.fail("lease remained held after child termination")
                        time.sleep(0.05)
                    else:
                        acquired.close()
                        break
            finally:
                if proc.poll() is None:
                    terminate_process_group(proc, lambda _message: None, grace_seconds=0.1)
                if proc.stdout is not None:
                    proc.stdout.close()
                unregister_process(proc)


if __name__ == "__main__":
    unittest.main()
