from __future__ import annotations

import contextlib
import subprocess
import signal
import threading
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import local_agent.supervisor.serial as multi
from local_agent.repository.worker import WORKER_IDLE, WORKER_PROCESSED
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


class MultiRepositorySupervisorTests(unittest.TestCase):
    def setUp(self) -> None:
        state = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(mock.patch.object(multi.agentd, "STATE_DIR", state))
        self.enterContext(mock.patch.dict(multi.os.environ, {}))
        self.enterContext(mock.patch.object(multi, "_stop_deadline", None))
        self.enterContext(mock.patch.object(multi, "_stop_requested", threading.Event()))

    def test_ordered_repositories_rotates_after_last_served_repository(self) -> None:
        repositories = [repository("a"), repository("b"), repository("c")]
        ordered = multi.ordered_repositories(repositories, "a")
        self.assertEqual([item.repository_id for item in ordered], ["b", "c", "a"])
        ordered = multi.ordered_repositories(repositories, "c")
        self.assertEqual([item.repository_id for item in ordered], ["a", "b", "c"])

    def test_unknown_last_repository_falls_back_to_registry_order(self) -> None:
        repositories = [repository("a"), repository("b")]
        ordered = multi.ordered_repositories(repositories, "missing")
        self.assertEqual([item.repository_id for item in ordered], ["a", "b"])

    def test_adaptive_poll_tier_moves_hot_warm_idle(self) -> None:
        self.assertEqual(multi.adaptive_poll_tier(None, 100.0), ("idle", 15.0))
        self.assertEqual(multi.adaptive_poll_tier(100.0, 100.0), ("hot", 2.0))
        self.assertEqual(multi.adaptive_poll_tier(100.0, 129.9), ("hot", 2.0))
        self.assertEqual(multi.adaptive_poll_tier(100.0, 130.0), ("warm", 5.0))
        self.assertEqual(multi.adaptive_poll_tier(100.0, 219.9), ("warm", 5.0))
        self.assertEqual(multi.adaptive_poll_tier(100.0, 220.0), ("idle", 15.0))

    def test_scheduler_sleep_uses_active_repository_interval(self) -> None:
        delay = multi.scheduler_sleep_seconds(
            active_repository="a",
            last_activity_at=100.0,
            last_active_poll_at=100.0,
            last_full_scan_at=100.0,
            last_control_service_at=100.0,
            now=100.5,
        )
        self.assertAlmostEqual(delay, 1.5)

    def test_scheduler_sleep_wakes_for_overdue_background_scan(self) -> None:
        delay = multi.scheduler_sleep_seconds(
            active_repository="a",
            last_activity_at=100.0,
            last_active_poll_at=100.0,
            last_full_scan_at=0.0,
            last_control_service_at=0.0,
            now=100.5,
        )
        self.assertAlmostEqual(delay, 0.0)

    def test_scheduler_sleep_uses_background_cadence_when_idle(self) -> None:
        delay = multi.scheduler_sleep_seconds(
            active_repository=None,
            last_activity_at=None,
            last_active_poll_at=None,
            last_full_scan_at=100.0,
            last_control_service_at=113.0,
            now=114.0,
        )
        self.assertAlmostEqual(delay, 1.0)

    def test_due_full_scan_suppresses_continuously_hot_repository_poll(self) -> None:
        actions = multi.scheduler_due_actions(
            active_repository="a",
            last_activity_at=100.0,
            last_active_poll_at=100.0,
            last_full_scan_at=80.0,
            last_control_service_at=100.0,
            now=102.0,
        )
        self.assertEqual(actions, ("full_scan",))

    def test_due_control_and_full_scan_are_selected_before_hot_poll(self) -> None:
        actions = multi.scheduler_due_actions(
            active_repository="a",
            last_activity_at=100.0,
            last_active_poll_at=100.0,
            last_full_scan_at=80.0,
            last_control_service_at=80.0,
            now=102.0,
        )
        self.assertEqual(actions, ("control", "full_scan"))

    def test_worker_command_targets_exact_repository_with_shared_hardened_worker(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            registry = Path(tmp) / "repositories.json"
            command = multi.worker_command(repository("a"), registry_path=registry)
        self.assertEqual(command[1:3], ["-m", "local_agent.supervisor.worker"])
        self.assertEqual(command[3:5], ["--repository-id", "a"])
        self.assertIn("--expected-config-digest", command)
        self.assertEqual(command[-2:], ["--registry", str(registry)])

    def test_active_repository_cycle_polls_only_requested_repository(self) -> None:
        repositories = [repository("a"), repository("b"), repository("c")]
        with mock.patch.object(
            multi, "load_repository_registry", return_value=repositories
        ), mock.patch.object(
            multi, "run_worker", return_value=WORKER_IDLE
        ) as run_worker:
            processed = multi.run_repository_cycle("b", registry_path=None)
        self.assertFalse(processed)
        run_worker.assert_called_once_with(repositories[1], registry_path=None)

    def test_active_repository_cycle_reports_processed_task(self) -> None:
        repositories = [repository("a"), repository("b")]
        with mock.patch.object(
            multi, "load_repository_registry", return_value=repositories
        ), mock.patch.object(
            multi, "run_worker", return_value=WORKER_PROCESSED
        ) as run_worker:
            processed = multi.run_repository_cycle("a", registry_path=None)
        self.assertTrue(processed)
        run_worker.assert_called_once_with(repositories[0], registry_path=None)

    def test_active_repository_cycle_treats_resource_busy_as_deferred(self) -> None:
        repositories = [repository("a")]
        for return_code in (multi.WORKER_RESOURCE_BUSY, multi.WORKER_MACHINE_BUSY):
            with self.subTest(return_code=return_code), mock.patch.object(
                multi, "load_repository_registry", return_value=repositories
            ), mock.patch.object(multi, "run_worker", return_value=return_code):
                self.assertFalse(multi.run_repository_cycle("a", registry_path=None))

    def test_cycle_continues_after_repository_worker_failure(self) -> None:
        repositories = [repository("a"), repository("b"), repository("c")]
        with mock.patch.object(
            multi, "load_repository_registry", return_value=repositories
        ), mock.patch.object(
            multi,
            "run_worker",
            side_effect=[3, WORKER_PROCESSED],
        ) as run_worker:
            processed, last_repository = multi.run_cycle(
                registry_path=None,
                start_after=None,
            )
        self.assertTrue(processed)
        self.assertEqual(last_repository, "b")
        self.assertEqual(
            [call.args[0].repository_id for call in run_worker.call_args_list],
            ["a", "b"],
        )

    def test_cycle_is_round_robin_and_stops_after_one_processed_task(self) -> None:
        repositories = [repository("a"), repository("b"), repository("c")]
        with mock.patch.object(
            multi, "load_repository_registry", return_value=repositories
        ), mock.patch.object(
            multi,
            "run_worker",
            side_effect=[WORKER_IDLE, WORKER_PROCESSED],
        ) as run_worker:
            processed, last_repository = multi.run_cycle(
                registry_path=None,
                start_after="a",
            )
        self.assertTrue(processed)
        self.assertEqual(last_repository, "c")
        self.assertEqual(
            [call.args[0].repository_id for call in run_worker.call_args_list],
            ["b", "c"],
        )

    def test_cycle_treats_resource_busy_as_deferred_and_continues(self) -> None:
        repositories = [repository("a"), repository("b")]
        with mock.patch.object(
            multi, "load_repository_registry", return_value=repositories
        ), mock.patch.object(
            multi,
            "run_worker",
            side_effect=[multi.WORKER_RESOURCE_BUSY, WORKER_PROCESSED],
        ) as run_worker:
            processed, last_repository = multi.run_cycle(
                registry_path=None,
                start_after=None,
            )
        self.assertTrue(processed)
        self.assertEqual(last_repository, "b")
        self.assertEqual(
            [call.args[0].repository_id for call in run_worker.call_args_list],
            ["a", "b"],
        )

    def test_supervisor_control_uses_first_repository(self) -> None:
        repositories = [repository("a"), repository("b")]
        with mock.patch.object(
            multi, "load_repository_registry", return_value=repositories
        ):
            selected = multi.supervisor_control_repository(registry_path=None)
        self.assertEqual(selected.repository_id, "a")

    def test_service_supervisor_control_syncs_and_handles_global_actions(self) -> None:
        target = repository("a")
        original_version = multi.agentd.DAEMON_VERSION
        original_control = multi.agentd.core.CONTROL
        original_branch = multi.agentd.core.CONTROL_BRANCH
        try:
            with mock.patch.object(multi, "sync_control_quietly") as sync, mock.patch.object(
                multi.agentd, "handle_control_request"
            ) as handle, mock.patch.object(multi.agentd, "maybe_self_update") as update:
                multi.service_supervisor_control(target)
            self.assertEqual(multi.agentd.core.CONTROL, target.control)
            self.assertEqual(multi.agentd.DAEMON_VERSION, multi.SUPERVISOR_VERSION)
            sync.assert_called_once_with()
            handle.assert_called_once_with(
                status_extra=multi.supervisor_status_fields(target)
            )
            update.assert_called_once_with()
        finally:
            multi.agentd.DAEMON_VERSION = original_version
            multi.agentd.core.CONTROL = original_control
            multi.agentd.core.CONTROL_BRANCH = original_branch

    def test_host_control_never_self_updates_from_main(self) -> None:
        from dataclasses import replace
        target = replace(repository("a"), host_id="worker-a", host_mailbox_enabled=True)
        with mock.patch.object(multi, "sync_control_quietly"), \
             mock.patch.object(multi.agentd, "handle_control_request") as handle, \
             mock.patch.object(multi.agentd, "maybe_self_update") as update:
            multi.service_supervisor_control(target)
        handle.assert_called_once()
        update.assert_not_called()
        multi.bind_supervisor_control(repository("a"))

    def test_service_supervisor_control_can_reuse_recent_worker_sync(self) -> None:
        target = repository("a")
        with mock.patch.object(multi, "sync_control_quietly") as sync, mock.patch.object(
            multi.agentd, "handle_control_request"
        ) as handle, mock.patch.object(multi.agentd, "maybe_self_update") as update:
            multi.service_supervisor_control(target, sync=False)
        sync.assert_not_called()
        handle.assert_called_once_with(
            status_extra=multi.supervisor_status_fields(target)
        )
        update.assert_called_once_with()

    def test_control_service_failure_is_degraded_without_raising(self) -> None:
        target = repository("a")
        with mock.patch.object(
            multi, "service_supervisor_control", side_effect=RuntimeError("network down")
        ), mock.patch.object(
            multi,
            "repository_execution_lease",
            return_value=contextlib.nullcontext(),
        ):
            self.assertFalse(multi.service_supervisor_control_safely(target))

    def test_worker_turn_timeout_terminates_the_process_group(self) -> None:
        target = repository("a")
        proc = mock.Mock(pid=12345)
        proc.wait.side_effect = subprocess.TimeoutExpired(["worker"], 1)
        with mock.patch.object(
            multi, "popen_registered", return_value=proc
        ), mock.patch.object(
            multi,
            "repository_execution_lease",
            return_value=contextlib.nullcontext(),
        ), mock.patch.object(multi, "unregister_process"), mock.patch.object(
            multi,
            "terminate_process_group",
        ) as terminate:
            with mock.patch.object(multi.time, "monotonic", side_effect=[0.0, 0.0, 1e9]):
                return_code = multi.run_worker(target, registry_path=None)
        self.assertEqual(return_code, 124)
        terminate.assert_called_once_with(proc, multi.log)

    def test_normal_stop_closes_admission_without_terminating_current_worker(self) -> None:
        with mock.patch.object(multi, "terminate_active_processes") as terminate:
            multi.shutdown_handler(signal.SIGTERM, None)
            first_deadline = multi._stop_deadline
            multi.shutdown_handler(signal.SIGTERM, None)
        self.assertTrue(multi._stop_requested.is_set())
        self.assertEqual(multi._stop_deadline, first_deadline)
        terminate.assert_not_called()
        with mock.patch.object(multi, "popen_registered") as spawn:
            self.assertEqual(multi.run_worker(repository("a"), registry_path=None), WORKER_IDLE)
        spawn.assert_not_called()

    def test_normal_stop_does_not_start_another_repository_turn(self) -> None:
        def finish_turn(*_args, **_kwargs):
            multi.shutdown_handler(signal.SIGTERM, None)
            return WORKER_IDLE

        with mock.patch.object(multi, "load_repository_registry", return_value=[repository("a"), repository("b")]), mock.patch.object(multi, "run_worker", side_effect=finish_turn) as worker:
            multi.run_cycle(registry_path=None, start_after=None)
        self.assertEqual(worker.call_count, 1)

    def test_emergency_stop_terminates_registered_processes(self) -> None:
        with mock.patch.object(multi, "defer_termination", return_value=False), \
             mock.patch.object(multi, "terminate_active_processes") as terminate:
            with self.assertRaises(SystemExit) as raised:
                multi.shutdown_handler(signal.SIGINT, None)
        self.assertEqual(raised.exception.code, 128 + signal.SIGINT)
        terminate.assert_called_once_with(multi.log)

    def test_inherited_stop_prevents_reexec_from_starting_a_worker(self) -> None:
        with mock.patch.dict(multi.os.environ, {multi.INHERITED_STOP_ENV: '1'}), mock.patch.object(multi.agentd, 'acquire_daemon_lock') as lock, mock.patch.object(multi, 'run_worker') as worker:
            self.assertEqual(multi.main(), 0)
        lock.assert_not_called()
        worker.assert_not_called()

    def test_pending_stop_prevents_supervisor_restart(self) -> None:
        multi._stop_requested.set()
        with mock.patch.object(multi.os, 'execv') as execute:
            with self.assertRaises(SystemExit) as raised:
                multi.restart_supervisor('fixture', registry_path=None, once=False)
        self.assertEqual(raised.exception.code, 0)
        execute.assert_not_called()

    def test_stop_deadline_interrupts_worker_without_extending_turn_budget(self) -> None:
        target = repository("a")
        proc = mock.Mock(pid=12345)

        def wait(*_args, **_kwargs):
            multi._stop_requested.set()
            multi._stop_deadline = 1.0
            raise subprocess.TimeoutExpired(["worker"], 0.5)

        proc.wait.side_effect = wait
        with mock.patch.object(multi, "popen_registered", return_value=proc), mock.patch.object(multi, "repository_execution_lease", return_value=contextlib.nullcontext()), mock.patch.object(multi, "unregister_process"), mock.patch.object(multi, "terminate_process_group") as terminate, mock.patch.object(multi.time, "monotonic", side_effect=[0.0, 0.0, 2.0]):
            code = multi.run_worker(target, registry_path=None)
        self.assertEqual(code, 128 + signal.SIGTERM)
        terminate.assert_called_once_with(proc, multi.log)

    def test_all_idle_preserves_last_repository_cursor(self) -> None:
        repositories = [repository("a"), repository("b")]
        with mock.patch.object(
            multi, "load_repository_registry", return_value=repositories
        ), mock.patch.object(
            multi,
            "run_worker",
            side_effect=[WORKER_IDLE, WORKER_IDLE],
        ):
            processed, last_repository = multi.run_cycle(
                registry_path=None,
                start_after="a",
            )
        self.assertFalse(processed)
        self.assertEqual(last_repository, "a")


if __name__ == "__main__":
    unittest.main()
