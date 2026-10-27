from __future__ import annotations

import argparse
import json
import os
import signal
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import local_agent.foundation.core as core
import local_agent.operator.local as agent_operator
import local_agent.supervisor.orchestrator as agent_parallel
import local_agent.supervisor.worker as agent_parallel_worker
import local_agent.repository.worker as worker
import local_agent.daemon.service as agentd
from local_agent.repository.context import RepositoryContext


class OperatorStateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.state_dir = root / "state"
        self.disabled_path = self.state_dir / "disabled.json"
        self.state_patch = mock.patch.object(agent_operator, "STATE_DIR", self.state_dir)
        self.path_patch = mock.patch.object(agent_operator, "DISABLED_PATH", self.disabled_path)
        self.state_patch.start()
        self.path_patch.start()

    def tearDown(self) -> None:
        self.path_patch.stop()
        self.state_patch.stop()
        self.tmp.cleanup()

    def test_disable_is_persistent_and_enable_is_explicit(self) -> None:
        payload = agent_operator.disable_agent(
            control_id="disable-1",
            repository_id="project-a",
            reason="remote_control",
        )
        self.assertTrue(agent_operator.is_disabled())
        self.assertEqual(payload["control_id"], "disable-1")
        self.assertEqual(payload["repository_id"], "project-a")
        self.assertEqual(agent_operator.disabled_state()["reason"], "remote_control")

        self.assertTrue(agent_operator.enable_agent())
        self.assertFalse(agent_operator.is_disabled())
        self.assertFalse(agent_operator.enable_agent())

    def test_malformed_disable_marker_fails_closed(self) -> None:
        self.disabled_path.parent.mkdir(parents=True)
        self.disabled_path.write_text("{broken", encoding="utf-8")
        self.assertTrue(agent_operator.is_disabled())
        self.assertEqual(agent_operator.disabled_state(), {})

    def test_operator_cli_enable_clears_marker(self) -> None:
        agent_operator.disable_agent(reason="test")
        result = agent_operator.command_enable(argparse.Namespace())
        self.assertEqual(result, 0)
        self.assertFalse(agent_operator.is_disabled())


class EmergencyRepositoryControlTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.repository = RepositoryContext(
            repository_id="project-a",
            repository="owner/project-a",
            control=root / "control",
            work=root / "work",
            checkpoints=root / "checkpoints",
        )
        (self.repository.control / ".git").mkdir(parents=True)
        (self.repository.work / ".git").mkdir(parents=True)
        self.disabled_path = root / "state" / "disabled.json"
        self.operator_state_patch = mock.patch.object(
            agent_operator,
            "STATE_DIR",
            self.disabled_path.parent,
        )
        self.operator_path_patch = mock.patch.object(
            agent_operator,
            "DISABLED_PATH",
            self.disabled_path,
        )
        self.operator_state_patch.start()
        self.operator_path_patch.start()
        worker.bind_repository(self.repository)

    def tearDown(self) -> None:
        self.operator_path_patch.stop()
        self.operator_state_patch.stop()
        self.tmp.cleanup()

    def write_control(self, payload: dict[str, object]) -> None:
        path = self.repository.control / agentd.REMOTE_CONTROL_REQUEST
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload), encoding="utf-8")

    def test_disable_control_persists_marker_and_acks(self) -> None:
        self.write_control({"id": "disable-1", "action": "disable"})
        with mock.patch.object(agentd, "control_ack_published", return_value=False), mock.patch.object(
            worker, "publish_repository_control_ack"
        ) as publish_ack:
            worker.handle_repository_control(self.repository)

        self.assertTrue(agent_operator.is_disabled())
        state = agent_operator.disabled_state()
        self.assertEqual(state["control_id"], "disable-1")
        self.assertEqual(state["repository_id"], "project-a")
        publish_ack.assert_called_once_with(
            self.repository,
            "disable-1",
            "disable",
            "completed",
            result="agent_disabled",
        )

    def test_cancel_pending_task_publishes_terminal_result(self) -> None:
        task = {"id": "task-1", "resources": []}
        with mock.patch.object(agentd, "pending_tasks", return_value=[(Path("task.json"), task)]), mock.patch.object(
            core, "publish_result"
        ) as publish_result, mock.patch.object(
            agentd, "publish_run_state"
        ), mock.patch.object(
            worker, "publish_repository_control_ack"
        ) as publish_ack:
            self.assertTrue(worker._cancel_pending_task(self.repository, "cancel-1", "task-1"))

        result = publish_result.call_args.args[1]
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["failure_reason"], "cancelled_by_operator")
        publish_ack.assert_called_once_with(
            self.repository,
            "cancel-1",
            "cancel_task",
            "completed",
            task_id="task-1",
            result="cancelled_before_execution",
        )

    def test_active_cancel_only_targets_matching_task(self) -> None:
        watcher = worker.ActiveRepositoryControlWatcher(self.repository, "active-task")
        request = {
            "id": "cancel-active",
            "action": "cancel_task",
            "task_id": "active-task",
        }
        with mock.patch.object(worker, "CONTROL_WATCH_SECONDS", 0.0), mock.patch.object(
            worker, "sync_control_quietly"
        ), mock.patch.object(
            worker, "_read_repository_control_request", return_value=request
        ), mock.patch.object(
            worker, "_control_already_acknowledged", return_value=False
        ), mock.patch.object(
            worker, "publish_repository_control_ack"
        ) as publish_ack, mock.patch.object(
            os, "kill"
        ) as kill:
            watcher._run()

        publish_ack.assert_called_once_with(
            self.repository,
            "cancel-active",
            "cancel_task",
            "accepted",
            task_id="active-task",
            result="terminating_active_task",
        )
        kill.assert_called_once_with(os.getpid(), signal.SIGTERM)

    def test_serial_worker_does_not_admit_task_when_disabled(self) -> None:
        with mock.patch.object(worker, "sync_control_quietly"), mock.patch.object(
            agentd, "recover_stale_claims"
        ), mock.patch.object(agentd, "recover_invalid_task_files"), mock.patch.object(
            worker, "handle_repository_control"
        ), mock.patch.object(agent_operator, "is_disabled", return_value=True), mock.patch.object(
            agentd, "pending_tasks"
        ) as pending, mock.patch.object(worker, "publish_repository_status") as publish_status:
            self.assertFalse(worker.poll_repository_once(self.repository))

        pending.assert_not_called()
        publish_status.assert_called_once_with(
            self.repository,
            "disabled",
            force_remote=True,
        )

    def test_parallel_worker_does_not_admit_task_when_disabled(self) -> None:
        with mock.patch.object(worker, "sync_control_quietly"), mock.patch.object(
            agentd, "recover_stale_claims"
        ), mock.patch.object(agentd, "recover_invalid_task_files"), mock.patch.object(
            worker, "handle_repository_control"
        ), mock.patch.object(agent_operator, "is_disabled", return_value=True), mock.patch.object(
            agentd, "pending_tasks"
        ) as pending, mock.patch.object(worker, "publish_repository_status") as publish_status:
            self.assertFalse(agent_parallel_worker.poll_repository_once(self.repository))

        pending.assert_not_called()
        publish_status.assert_called_once_with(
            self.repository,
            "disabled",
            force_remote=True,
            execution_variant="parallel",
        )


class DisabledSupervisorTests(unittest.TestCase):
    def test_once_supervisor_never_starts_worker_while_disabled(self) -> None:
        repository = RepositoryContext(
            repository_id="project-a",
            repository="owner/project-a",
            control=Path("/tmp/control-a"),
            work=Path("/tmp/work-a"),
            checkpoints=Path("/tmp/checkpoints-a"),
        )
        args = argparse.Namespace(registry=Path("/tmp/registry.json"), max_workers=2, once=True)
        with mock.patch.object(agent_parallel, "parse_args", return_value=args), mock.patch.object(
            agent_parallel.scheduling, "resolve_max_workers", return_value=2
        ), mock.patch.object(agentd, "acquire_daemon_lock", return_value=object()), mock.patch.object(
            agent_parallel, "install_signal_handlers"
        ), mock.patch.object(
            agent_parallel, "load_repository_registry", return_value=[repository]
        ), mock.patch.object(
            agent_operator, "is_disabled", return_value=True
        ), mock.patch.object(
            agent_parallel, "publish_local_supervisor_status"
        ) as publish_status, mock.patch.object(
            agent_parallel, "start_worker"
        ) as start_worker:
            self.assertEqual(agent_parallel.main(), 0)

        start_worker.assert_not_called()
        publish_status.assert_called_once_with(
            {},
            max_workers=2,
            state="disabled",
        )


if __name__ == "__main__":
    unittest.main()
