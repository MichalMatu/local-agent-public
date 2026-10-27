"""Parallel supervisor worker scheduling contract for independently authorized hosts."""
from __future__ import annotations

import argparse
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from local_agent.supervisor import orchestrator
from local_agent.foundation import storage
from local_agent.supervisor.host_control import HostControlSource
from local_agent.supervisor.host_remote_worker import RemoteIntakeConfig


class RemoteHostSupervisorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.source = HostControlSource(
            "worker-mac", "12345678-1234-4234-8234-123456789abc",
            Path("/private/tmp/isolated-agent-control-fixture"),
        )

    def test_host_worker_uses_shared_process_pool_without_project_binding(self) -> None:
        proc = SimpleNamespace(pid=24850, poll=lambda: None)
        running = {}
        with (
            mock.patch.object(orchestrator.agent_operator, "is_disabled", return_value=False),
            mock.patch.object(orchestrator, "popen_registered", return_value=proc) as spawn,
            mock.patch.object(orchestrator, "publish_local_supervisor_status"),
        ):
            self.assertTrue(orchestrator.start_host_task_worker(
                self.source, "command-one", running=running, max_workers=2,
            ))
        self.assertIn("@host:command-one", running)
        command = spawn.call_args.args[0]
        self.assertEqual(command[:3], [
            orchestrator.sys.executable, "-m",
            "local_agent.supervisor.host_remote_worker",
        ])
        self.assertIn("--execute", command)
        self.assertIn("--operator-checkout", command)
        self.assertNotIn("--repository-id", command)
        self.assertNotIn("--work-branch", command)

    def test_machine_owned_transport_is_not_legacy_project_control(self) -> None:
        self.assertTrue(orchestrator.machine_control_is_host_owned(self.source))
        self.assertFalse(self.source.host_mailbox_enabled)

    def test_host_result_recovery_is_scoped_to_exact_installation(self) -> None:
        ns = self.source.namespace
        dirs, legacy_gc = storage._recoverable_control_paths(f"{ns}/tasks")
        self.assertFalse(legacy_gc)
        self.assertIn(f"{ns}/task-results", dirs)
        self.assertNotIn(".agent/results", dirs)
        other = ns.replace(self.source.installation_id,
                           "12345678-1234-4234-8234-123456789abd")
        self.assertNotIn(f"{other}/task-results", dirs)
        project_dirs, _ = storage._recoverable_control_paths(
            ".agent/hosts/worker-mac/tasks",
        )
        self.assertNotIn(".agent/hosts/worker-mac/task-results", project_dirs)

    def test_host_worker_respects_emergency_disable_capacity_and_running_slot(self) -> None:
        with (
            mock.patch.object(orchestrator, "popen_registered") as spawn,
            mock.patch.object(orchestrator, "publish_local_supervisor_status"),
        ):
            with mock.patch.object(
                orchestrator.agent_operator, "is_disabled", return_value=True,
            ):
                self.assertFalse(orchestrator.start_host_task_worker(
                    self.source, "command-one", running={}, max_workers=2,
                ))
            with mock.patch.object(
                orchestrator.agent_operator, "is_disabled", return_value=False,
            ):
                occupied = {"other": SimpleNamespace()}
                self.assertFalse(orchestrator.start_host_task_worker(
                    self.source, "command-one", running=occupied, max_workers=1,
                ))
                running = {"@host:command-one": SimpleNamespace()}
                self.assertFalse(orchestrator.start_host_task_worker(
                    self.source, "command-one", running=running, max_workers=2,
                ))
        spawn.assert_not_called()

    def test_host_resource_deferral_uses_resource_backoff(self) -> None:
        from local_agent.supervisor import host_remote_worker, scheduling

        proc = SimpleNamespace(poll=lambda: host_remote_worker.HOST_WORKER_RESOURCE_DEFERRED)
        running = {"@host:busy-device": orchestrator.RunningWorker(
            repository_id="@host:busy-device", proc=proc, started_at=99.0,
        )}
        schedules = {"@host:busy-device": scheduling.RepositorySchedule()}
        with (
            mock.patch.object(orchestrator, "unregister_process"),
            mock.patch.object(orchestrator.time, "monotonic", return_value=100.0),
        ):
            completed = orchestrator.reap_workers(running, schedules)
        self.assertEqual(completed["@host:busy-device"], host_remote_worker.HOST_WORKER_RESOURCE_DEFERRED)
        self.assertNotIn("@host:busy-device", running)
        self.assertEqual(schedules["@host:busy-device"].resource_deferrals, 1)
        self.assertEqual(schedules["@host:busy-device"].retry_not_before, 102.0)

    def test_host_control_deferrals_reset_failures_cap_retry_and_limit_logs(self) -> None:
        from local_agent.supervisor import host_remote_worker, scheduling

        key = "@host:busy-control"
        schedule = scheduling.RepositorySchedule(consecutive_failures=8, last_failure_code=2)
        with (
            mock.patch.object(orchestrator, "unregister_process"),
            mock.patch.object(orchestrator.time, "monotonic", return_value=100.0),
            mock.patch.object(orchestrator, "log") as log,
        ):
            for expected_delay in (2, 4, 8, 15, 15, 15):
                proc = SimpleNamespace(poll=lambda: host_remote_worker.HOST_WORKER_CONTROL_DEFERRED)
                running = {key: orchestrator.RunningWorker(key, proc, 99.0)}
                orchestrator.reap_workers(running, {key: schedule})
                self.assertFalse(running)
                self.assertEqual(schedule.retry_not_before, 100.0 + expected_delay)
                self.assertEqual(schedule.consecutive_failures, 0)
                self.assertIsNone(schedule.last_failure_code)
            log.assert_called_once()

    def test_publication_success_uses_shared_success_code_and_resets_backoff(self) -> None:
        from local_agent.supervisor import host_remote_worker, scheduling

        for key in ("@host:published", "ordinary-project"):
            schedule = scheduling.RepositorySchedule(
                consecutive_failures=8, last_failure_code=2, resource_deferrals=4,
                retry_not_before=400, last_deferral_log_at=99,
            )
            proc = SimpleNamespace(poll=lambda: host_remote_worker.HOST_WORKER_PUBLISHED)
            with (
                self.subTest(key=key),
                mock.patch.object(orchestrator, "unregister_process"),
                mock.patch.object(orchestrator.time, "monotonic", return_value=100.0),
                mock.patch.object(orchestrator, "log"),
            ):
                orchestrator.reap_workers(
                    {key: orchestrator.RunningWorker(key, proc, 99.0)}, {key: schedule},
                )
            self.assertEqual(schedule.consecutive_failures, 0)
            self.assertEqual(schedule.retry_not_before, 0)
            self.assertEqual(schedule.last_activity_at, 100)
            self.assertEqual(schedule.resource_deferrals, 0)
            self.assertIsNone(schedule.last_deferral_log_at)

    def test_zero_project_supervisor_can_schedule_opted_in_host_task(self) -> None:
        args = argparse.Namespace(registry=Path("/unused-registry"), max_workers=1, once=True)
        calls: list[str] = []
        with (
            mock.patch.object(orchestrator, "parse_args", return_value=args),
            mock.patch.object(orchestrator.scheduling, "resolve_max_workers", return_value=1),
            mock.patch.object(orchestrator.agentd, "acquire_daemon_lock", return_value=object()),
            mock.patch.object(orchestrator, "install_signal_handlers"),
            mock.patch.object(orchestrator, "load_repository_registry", return_value=[]),
            mock.patch.object(orchestrator.host_supervisor_control, "load_host_control_source",
                              return_value=self.source),
            mock.patch.object(orchestrator.host_supervisor_control, "selected_host_control_source",
                              return_value=self.source),
            mock.patch.object(orchestrator, "reap_workers", return_value={}),
            mock.patch.object(orchestrator, "service_control", return_value=True),
            mock.patch.object(orchestrator, "publish_local_supervisor_status"),
            mock.patch.object(orchestrator, "quiesce_deferred_shutdown", return_value=False),
            mock.patch.object(orchestrator.agent_operator, "is_disabled", return_value=False),
            mock.patch.object(orchestrator.conversation_supervisor, "start_if_pending",
                              return_value=None),
            mock.patch.object(orchestrator.conversation_supervisor, "result_publish_pending",
                              return_value=False),
            mock.patch.object(orchestrator.scheduling, "local_log_maintenance_due",
                              return_value=False),
            mock.patch.object(orchestrator.host_remote_worker, "load_remote_intake_config",
                              return_value=RemoteIntakeConfig(Path("/private/identity"))),
            mock.patch.object(orchestrator.host_remote_worker, "pending_host_task_ids",
                              return_value=("command-one",)),
            mock.patch.object(orchestrator, "start_host_task_worker",
                              side_effect=lambda *a, **kw: calls.append(a[1]) or True),
        ):
            self.assertEqual(orchestrator.main(), 0)
        self.assertEqual(calls, ["command-one"])

    def test_host_task_wins_next_shared_worker_slot_before_project_poll(self) -> None:
        """An already-waiting Host Ops task must not starve behind new projects."""
        args = argparse.Namespace(registry=Path("/unused-registry"), max_workers=1, once=True)
        repository = SimpleNamespace(
            repository_id="project-a",
            control=Path("/private/tmp/test-control"),
            work=Path("/private/tmp/test-work"),
            checkpoints=Path("/private/tmp/test-checkpoints"),
        )
        started: list[str] = []

        def start_project(_repository, *, running, **_kwargs):
            if len(running) >= 1:
                return False
            started.append("project")
            running["project-a"] = SimpleNamespace()
            return True

        def start_host(_source, _task_id, *, running, **_kwargs):
            if len(running) >= 1:
                return False
            started.append("host")
            running["@host:command-one"] = SimpleNamespace()
            return True

        with (
            mock.patch.object(orchestrator, "parse_args", return_value=args),
            mock.patch.object(orchestrator.scheduling, "resolve_max_workers", return_value=1),
            mock.patch.object(orchestrator.agentd, "acquire_daemon_lock", return_value=object()),
            mock.patch.multiple(orchestrator, install_signal_handlers=mock.DEFAULT,
                                publish_local_supervisor_status=mock.DEFAULT),
            mock.patch.object(orchestrator, "load_repository_registry", return_value=[repository]),
            mock.patch.object(orchestrator.host_supervisor_control, "load_host_control_source",
                              return_value=self.source),
            mock.patch.object(orchestrator.host_supervisor_control, "selected_host_control_source",
                              return_value=self.source),
            mock.patch.object(orchestrator, "reap_workers", return_value={}),
            mock.patch.object(orchestrator, "service_control", return_value=True),
            mock.patch.object(orchestrator, "quiesce_deferred_shutdown", return_value=False),
            mock.patch.object(orchestrator.agent_operator, "is_disabled", return_value=False),
            mock.patch.object(orchestrator.conversation_supervisor, "start_if_pending",
                              return_value=None),
            mock.patch.object(orchestrator.conversation_supervisor, "result_publish_pending",
                              return_value=False),
            mock.patch.object(orchestrator.scheduling, "local_log_maintenance_due",
                              return_value=False),
            mock.patch.object(orchestrator.host_remote_worker, "load_remote_intake_config",
                              return_value=RemoteIntakeConfig(Path("/private/identity"))),
            mock.patch.object(orchestrator.host_remote_worker, "pending_host_task_ids",
                              return_value=("command-one",)),
            mock.patch.object(orchestrator, "start_worker", side_effect=start_project),
            mock.patch.object(orchestrator, "start_host_task_worker", side_effect=start_host),
            mock.patch.object(orchestrator.time, "sleep", side_effect=SystemExit(0)),
        ):
            with self.assertRaises(SystemExit):
                orchestrator.main()
        self.assertEqual(started, ["host"])

    def test_zero_project_supervisor_does_not_start_host_work_without_opt_in(self) -> None:
        args = argparse.Namespace(registry=None, max_workers=1, once=True)
        with (
            mock.patch.object(orchestrator, "parse_args", return_value=args),
            mock.patch.object(orchestrator.scheduling, "resolve_max_workers", return_value=1),
            mock.patch.object(orchestrator.agentd, "acquire_daemon_lock", return_value=object()),
            mock.patch.object(orchestrator, "install_signal_handlers"),
            mock.patch.object(orchestrator, "load_repository_registry", return_value=[]),
            mock.patch.object(orchestrator.host_supervisor_control, "load_host_control_source",
                              return_value=self.source),
            mock.patch.object(orchestrator.host_supervisor_control, "selected_host_control_source",
                              return_value=self.source),
            mock.patch.object(orchestrator, "reap_workers", return_value={}),
            mock.patch.object(orchestrator, "service_control", return_value=True),
            mock.patch.object(orchestrator, "publish_local_supervisor_status"),
            mock.patch.object(orchestrator, "quiesce_deferred_shutdown", return_value=False),
            mock.patch.object(orchestrator.agent_operator, "is_disabled", return_value=False),
            mock.patch.object(orchestrator.conversation_supervisor, "start_if_pending",
                              return_value=None),
            mock.patch.object(orchestrator.conversation_supervisor, "result_publish_pending",
                              return_value=False),
            mock.patch.object(orchestrator.scheduling, "local_log_maintenance_due",
                              return_value=False),
            mock.patch.object(orchestrator.host_remote_worker, "load_remote_intake_config",
                              return_value=None),
            mock.patch.object(orchestrator, "start_host_task_worker") as launch,
        ):
            self.assertEqual(orchestrator.main(), 0)
        launch.assert_not_called()


if __name__ == "__main__":
    unittest.main()
