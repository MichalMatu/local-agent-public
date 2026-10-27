from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import local_agent.foundation.core as core
import local_agent.repository.worker as worker
import local_agent.daemon.service as agentd
from local_agent.repository.context import RepositoryContext


class RepositoryControlTests(unittest.TestCase):
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
        self.original_control = core.CONTROL
        self.original_work = core.WORK
        self.original_checkpoints = core.CHECKPOINTS
        self.original_control_branch = core.CONTROL_BRANCH
        self.original_state_dir = agentd.STATE_DIR
        self.original_claims_dir = agentd.CLAIMS_DIR
        self.original_corrupt_claims_dir = agentd.CORRUPT_CLAIMS_DIR
        self.original_local_status_path = agentd.LOCAL_STATUS_PATH
        self.original_local_runs_dir = agentd.LOCAL_RUNS_DIR
        self.original_result_spool_dir = agentd.RESULT_SPOOL_DIR
        agentd.STATE_DIR = root / "state"
        self.original_last_remote_status = agentd._last_remote_status
        self.original_last_status_state = agentd._last_status_state
        agentd._last_remote_status = 0.0
        agentd._last_status_state = None

    def tearDown(self) -> None:
        core.CONTROL = self.original_control
        core.WORK = self.original_work
        core.CHECKPOINTS = self.original_checkpoints
        core.CONTROL_BRANCH = self.original_control_branch
        agentd.STATE_DIR = self.original_state_dir
        agentd.CLAIMS_DIR = self.original_claims_dir
        agentd.CORRUPT_CLAIMS_DIR = self.original_corrupt_claims_dir
        agentd.LOCAL_STATUS_PATH = self.original_local_status_path
        agentd.LOCAL_RUNS_DIR = self.original_local_runs_dir
        agentd.RESULT_SPOOL_DIR = self.original_result_spool_dir
        agentd._last_remote_status = self.original_last_remote_status
        agentd._last_status_state = self.original_last_status_state
        self.tmp.cleanup()

    def write_control(self, payload: dict) -> None:
        path = self.repository.control / agentd.REMOTE_CONTROL_REQUEST
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload), encoding="utf-8")

    def test_status_control_is_repository_scoped(self) -> None:
        worker.bind_repository(self.repository)
        self.write_control({"id": "status-1", "action": "status"})
        with mock.patch.object(agentd, "control_ack_published", return_value=False), mock.patch.object(
            worker, "publish_repository_status"
        ) as publish_status, mock.patch.object(
            worker, "publish_repository_control_ack"
        ) as publish_ack:
            worker.handle_repository_control(self.repository)
        publish_status.assert_called_once_with(
            self.repository,
            "idle",
            force_remote=True,
            control_id="status-1",
        )
        publish_ack.assert_called_once_with(
            self.repository,
            "status-1",
            "status",
            "completed",
            result="status_published",
        )

    def test_status_control_ignores_local_only_ack(self) -> None:
        worker.bind_repository(self.repository)
        self.write_control({"id": "status-local", "action": "status"})
        ack = self.repository.control / agentd.REMOTE_CONTROL_ACK_DIR / "status-local.json"
        ack.parent.mkdir(parents=True, exist_ok=True)
        ack.write_text("{}\n", encoding="utf-8")
        with mock.patch.object(agentd, "control_ack_published", return_value=False), mock.patch.object(
            worker, "publish_repository_status"
        ) as publish_status, mock.patch.object(
            worker, "publish_repository_control_ack"
        ) as publish_ack:
            worker.handle_repository_control(self.repository)
        publish_status.assert_called_once()
        publish_ack.assert_called_once()

    def test_restart_is_left_for_supervisor_without_ack(self) -> None:
        worker.bind_repository(self.repository)
        self.write_control({"id": "restart-1", "action": "restart"})
        with mock.patch.object(worker, "publish_repository_control_ack") as publish_ack, mock.patch.object(
            agentd, "restart_self"
        ) as restart:
            worker.handle_repository_control(self.repository)
        restart.assert_not_called()
        publish_ack.assert_not_called()

    def test_self_update_is_left_for_supervisor_without_ack(self) -> None:
        worker.bind_repository(self.repository)
        self.write_control({"id": "self-update-1", "action": "self_update"})
        with mock.patch.object(worker, "publish_repository_control_ack") as publish_ack, mock.patch.object(
            agentd, "maybe_self_update"
        ) as self_update:
            worker.handle_repository_control(self.repository)
        self_update.assert_not_called()
        publish_ack.assert_not_called()

    def test_status_fields_include_supervisor_pid_when_present(self) -> None:
        with mock.patch.dict(os.environ, {"LOCAL_AGENT_SUPERVISOR_PID": "1234"}):
            fields = worker.repository_status_fields(self.repository)
        self.assertEqual(fields["repository_id"], "project-a")
        self.assertEqual(fields["repository"], "owner/project-a")
        self.assertEqual(fields["supervisor_pid"], 1234)
        self.assertEqual(fields["execution_model"], "multi_repository_worker")

    def test_idle_status_is_persisted_without_repeated_remote_commit(self) -> None:
        worker.bind_repository(self.repository)
        remote_status: dict[str, object] = {}

        def remember_remote_status(_relative, payload, **_kwargs):
            remote_status.clear()
            remote_status.update(payload)

        with mock.patch.object(
            agentd, "DAEMON_VERSION", worker.MULTIREPO_DAEMON_VERSION
        ), mock.patch.object(
            worker, "_remote_repository_status", side_effect=lambda: dict(remote_status)
        ), mock.patch.object(
            agentd, "publish_control_json", side_effect=remember_remote_status
        ) as publish:
            worker.publish_repository_status(self.repository, "idle", force_remote=False)
            worker.publish_repository_status(self.repository, "idle", force_remote=False)
        self.assertEqual(publish.call_count, 1)
        payload = json.loads(agentd.LOCAL_STATUS_PATH.read_text(encoding="utf-8"))
        self.assertEqual(payload["repository_id"], "project-a")
        self.assertEqual(payload["state"], "idle")

    def test_worker_turn_restores_legacy_daemon_version(self) -> None:
        original = agentd.DAEMON_VERSION
        with mock.patch.object(worker, "sync_control_quietly"), mock.patch.object(
            agentd, "recover_stale_claims"
        ), mock.patch.object(agentd, "recover_invalid_task_files"), mock.patch.object(
            worker, "handle_repository_control"
        ), mock.patch.object(agentd, "pending_tasks", return_value=[]), mock.patch.object(
            worker, "publish_repository_status"
        ):
            self.assertFalse(worker.poll_repository_once(self.repository))
        self.assertEqual(agentd.DAEMON_VERSION, original)


if __name__ == "__main__":
    unittest.main()
