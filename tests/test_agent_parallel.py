from __future__ import annotations

import contextlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import local_agent.supervisor.orchestrator as parallel
import local_agent.repository.worker as serial_worker
from local_agent.foundation.process import ExecutionLeaseBusy
from local_agent.repository.context import RepositoryContext


def repository(repository_id: str) -> RepositoryContext:
    root = Path("/tmp") / repository_id
    return RepositoryContext(
        repository_id=repository_id,
        repository=f"owner/{repository_id}",
        control=root / "control",
        work=root / "work",
        checkpoints=root / "checkpoints",
    )


class ParallelSupervisorTests(unittest.TestCase):
    def test_failed_worker_admission_announces_activity_and_restores_idle(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            status = Path(tmp) / "status.json"
            for error in (ExecutionLeaseBusy("busy"), OSError("spawn failed")):
                with self.subTest(error=type(error).__name__):
                    running = {}

                    def checked_failure(*_args, **_kwargs):
                        payload = json.loads(status.read_text(encoding="utf-8"))
                        self.assertEqual(payload["state"], "running")
                        raise error

                    with mock.patch.object(parallel.agentd, "LOCAL_STATUS_PATH", status), \
                         mock.patch.object(parallel.agent_operator, "is_disabled", return_value=False), \
                         mock.patch.object(parallel, "log"), \
                         mock.patch.object(parallel.serial_worker, "repository_execution_lease",
                                           side_effect=checked_failure):
                        self.assertFalse(parallel.start_worker(
                            repository("a"), registry_path=None, running=running, max_workers=2,
                        ))
                    self.assertEqual(running, {})
                    self.assertEqual(json.loads(status.read_text())["state"], "idle")

    def test_spawn_failure_preserves_existing_worker_status(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            status = Path(tmp) / "status.json"
            slot = parallel.RunningWorker("existing", mock.Mock(), 1.0)
            running = {"existing": slot}
            with mock.patch.object(parallel.agentd, "LOCAL_STATUS_PATH", status), \
                 mock.patch.object(parallel.agent_operator, "is_disabled", return_value=False), \
                 mock.patch.object(parallel.serial_worker, "repository_execution_lease",
                                   return_value=contextlib.nullcontext()), \
                 mock.patch.object(parallel, "popen_registered", side_effect=OSError("spawn failed")), \
                 mock.patch.object(parallel, "log"):
                self.assertFalse(parallel.start_worker(
                    repository("a"), registry_path=None, running=running, max_workers=2,
                ))
            self.assertEqual(running, {"existing": slot})
            payload = json.loads(status.read_text())
            self.assertEqual(payload["state"], "running")
            self.assertEqual(payload["active_repository_ids"], ["existing"])

    def test_scheduler_quiesces_while_deferred_shutdown_is_pending(self) -> None:
        with mock.patch.object(
            parallel,
            "process_shutdown_requested",
            return_value=True,
        ), mock.patch.object(parallel.time, "sleep") as sleep:
            self.assertTrue(parallel.quiesce_deferred_shutdown())
        sleep.assert_called_once_with(parallel.REAP_INTERVAL_SECONDS)

    def test_default_concurrency_is_one(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(parallel.scheduling.resolve_max_workers(None), 1)

    def test_cli_concurrency_overrides_environment(self) -> None:
        with mock.patch.dict(
            os.environ,
            {parallel.scheduling.MAX_WORKERS_ENV: "2"},
            clear=False,
        ):
            self.assertEqual(parallel.scheduling.resolve_max_workers(4), 4)

    def test_invalid_concurrency_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            parallel.scheduling.resolve_max_workers(0)
        with self.assertRaises(ValueError):
            parallel.scheduling.resolve_max_workers(parallel.scheduling.MAX_MAX_WORKERS + 1)
        with mock.patch.dict(
            os.environ,
            {parallel.scheduling.MAX_WORKERS_ENV: "not-a-number"},
            clear=False,
        ):
            with self.assertRaises(ValueError):
                parallel.scheduling.resolve_max_workers(None)

    def test_parallel_concurrency_is_capped_at_four(self) -> None:
        self.assertEqual(parallel.scheduling.MAX_MAX_WORKERS, 4)

    def test_operator_idle_summary_is_human_readable(self) -> None:
        self.assertEqual(
            parallel.scheduling.format_operator_idle_summary(3, 2),
            "IDLE no active task (3 repositories); max_workers=2",
        )

    def test_operator_idle_heartbeat_is_periodic(self) -> None:
        self.assertTrue(parallel.scheduling.operator_idle_log_due(None, now=100.0))
        self.assertFalse(parallel.scheduling.operator_idle_log_due(100.0, now=399.9))
        self.assertTrue(parallel.scheduling.operator_idle_log_due(100.0, now=400.0))

    def test_local_log_maintenance_is_periodic(self) -> None:
        self.assertTrue(parallel.scheduling.local_log_maintenance_due(None, now=100.0))
        self.assertFalse(parallel.scheduling.local_log_maintenance_due(100.0, now=129.9))
        self.assertTrue(parallel.scheduling.local_log_maintenance_due(100.0, now=130.0))

    def test_compact_inherited_log_keeps_recent_tail_and_append_semantics(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "agent.log"
            newline = bytes((10,))
            lines = [
                f"{index:04d} payload payload payload".encode("utf-8") + newline
                for index in range(200)
            ]
            path.write_bytes(b"".join(lines))
            fd = os.open(path, os.O_WRONLY)
            try:
                os.lseek(fd, 0, os.SEEK_SET)
                self.assertTrue(
                    parallel.compact_inherited_log_file(
                        fd,
                        path,
                        max_bytes=1024,
                        keep_bytes=512,
                    )
                )
                compacted = path.read_bytes()
                self.assertLessEqual(len(compacted), 512)
                self.assertNotIn(lines[0], compacted)
                self.assertTrue(compacted.endswith(lines[-1]))

                os.lseek(fd, 0, os.SEEK_SET)
                os.write(fd, b"after-compaction" + newline)
                self.assertTrue(path.read_bytes().endswith(b"after-compaction" + newline))
            finally:
                os.close(fd)

    def test_compact_inherited_log_refuses_mismatched_descriptor(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            first = Path(tmp) / "first.log"
            second = Path(tmp) / "second.log"
            first.write_bytes(b"a" * 128)
            second.write_bytes(b"b" * 128)
            fd = os.open(first, os.O_WRONLY)
            try:
                self.assertFalse(
                    parallel.compact_inherited_log_file(
                        fd,
                        second,
                        max_bytes=64,
                        keep_bytes=32,
                    )
                )
                self.assertEqual(second.read_bytes(), b"b" * 128)
            finally:
                os.close(fd)

    def test_worker_command_uses_parallel_worker(self) -> None:
        command = parallel.worker_command(repository("a"), registry_path=None)
        self.assertEqual(command[1:3], ["-m", "local_agent.supervisor.worker"])
        self.assertEqual(command[3:5], ["--repository-id", "a"])
        self.assertIn("--expected-config-digest", command)

    def test_new_repository_is_due_immediately(self) -> None:
        self.assertTrue(
            parallel.scheduling.repository_due(
                parallel.scheduling.RepositorySchedule(),
                now=100.0,
            )
        )

    def test_retry_deadline_blocks_poll_until_due(self) -> None:
        schedule = parallel.scheduling.RepositorySchedule(last_poll_at=100.0, retry_not_before=101.0)
        self.assertFalse(parallel.scheduling.repository_due(schedule, now=100.9))
        self.assertTrue(parallel.scheduling.repository_due(schedule, now=101.0))

    def test_next_repository_delay_prefers_explicit_retry_deadline(self) -> None:
        target = repository("retry-delay")
        schedule = parallel.scheduling.RepositorySchedule(
            last_poll_at=100.0,
            retry_not_before=102.0,
        )
        delay = parallel.scheduling.next_repository_delay(
            {target.repository_id: schedule},
            [target.repository_id],
            now=100.0,
        )
        self.assertEqual(delay, 2.0)

    def test_recently_active_repository_uses_hot_polling(self) -> None:
        schedule = parallel.scheduling.RepositorySchedule(
            last_poll_at=100.0,
            last_activity_at=100.0,
        )
        self.assertFalse(parallel.scheduling.repository_due(schedule, now=101.9))
        self.assertTrue(parallel.scheduling.repository_due(schedule, now=102.0))

    def test_retry_helpers_and_reset(self) -> None:
        self.assertEqual(
            [parallel.scheduling.resource_retry_seconds(i) for i in range(1, 8)],
            [2.0, 5.0, 10.0, 30.0, 60.0, 60.0, 60.0],
        )
        self.assertEqual(
            [parallel.scheduling.worker_failure_retry_seconds(i) for i in range(1, 10)],
            [2.0, 4.0, 8.0, 16.0, 32.0, 64.0, 128.0, 256.0, 300.0],
        )
        self.assertEqual(
            [parallel.scheduling.control_defer_retry_seconds(i) for i in range(1, 6)],
            [2.0, 4.0, 8.0, 15.0, 15.0],
        )
        self.assertFalse(parallel.scheduling.control_lease_busy_should_force_drain(5))
        self.assertTrue(parallel.scheduling.control_lease_busy_should_force_drain(6))
        self.assertTrue(parallel.scheduling.control_lease_busy_should_force_drain(100))
        self.assertTrue(parallel.scheduling.repeated_failure_log_due(None, 100.0))
        self.assertFalse(parallel.scheduling.repeated_failure_log_due(100.0, 159.9))
        self.assertTrue(parallel.scheduling.repeated_failure_log_due(100.0, 160.0))

        schedule = parallel.scheduling.RepositorySchedule(
            consecutive_failures=4,
            last_failure_code=7,
            last_failure_log_at=100.0,
            resource_deferrals=4,
        )
        parallel.scheduling.reset_worker_failure_state(schedule)
        self.assertEqual(
            (
                schedule.consecutive_failures,
                schedule.last_failure_code,
                schedule.last_failure_log_at,
            ),
            (0, None, None),
        )
        parallel.scheduling.reset_resource_deferral_state(schedule)
        self.assertEqual(schedule.resource_deferrals, 0)

    def test_control_probe_distinguishes_lease_busy_from_degraded_probe(self) -> None:
        target = repository("control-probe")
        with mock.patch.object(
            serial_worker,
            "repository_execution_lease",
            side_effect=ExecutionLeaseBusy("repo:control-probe"),
        ):
            self.assertIs(
                parallel.probe_control_request(target),
                parallel.ControlProbeResult.LEASE_BUSY,
            )

    def test_global_control_requires_every_repository_lease(self) -> None:
        repositories = [repository("control-a"), repository("control-b")]
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(
            parallel.agentd,
            "STATE_DIR",
            Path(tmp),
        ):
            with serial_worker.repository_execution_lease(repositories[1]):
                with self.assertRaises(ExecutionLeaseBusy):
                    with parallel.supervisor_control_leases(repositories):
                        self.fail("global control acquired a busy repository")


if __name__ == "__main__":
    unittest.main()
