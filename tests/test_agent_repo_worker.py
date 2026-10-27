from __future__ import annotations

import json
import os
import signal
import tempfile
from datetime import datetime, timedelta, timezone
import unittest
from pathlib import Path
from unittest import mock

import local_agent.foundation.core as core
import local_agent.repository.binding as binding
import local_agent.repository.worker as worker
import local_agent.foundation.storage as storage
import local_agent.daemon.service as agentd
from local_agent.repository.context import RepositoryContext, repository_config_digest
from local_agent.supervisor import macos_release_admission as mac_admission

PROJECT_BINDING = "3da0947d-9acf-4ecf-adce-a29be7dc5c09"


class RepositoryWorkerTests(unittest.TestCase):
    def test_worker_version_matches_agent_release(self) -> None:
        self.assertEqual(worker.MULTIREPO_DAEMON_VERSION, agentd.DAEMON_VERSION)

    def test_shutdown_uses_runtime_aware_process_order(self) -> None:
        with mock.patch.object(
            worker, "defer_termination", return_value=False
        ), mock.patch.object(worker.core, "log"), mock.patch.object(
            agentd, "shutdown_runtime_processes"
        ) as shutdown:
            with self.assertRaisesRegex(SystemExit, str(128 + signal.SIGTERM)):
                worker.shutdown_handler(signal.SIGTERM, None)
        shutdown.assert_called_once_with()

    def setUp(self) -> None:
        # Fixture repositories are not enrolled Mac services. A native test
        # launched by the real daemon must not inherit its required-admission
        # policy into this otherwise isolated, non-production test state.
        admission_env = mock.patch.dict(
            os.environ, {mac_admission.REQUIRED_ENV: "0"},
        )
        admission_env.start()
        self.addCleanup(admission_env.stop)
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.repository = RepositoryContext(
            repository_id="project-a",
            repository="owner/project-a",
            control=root / "control",
            work=root / "work",
            checkpoints=root / "checkpoints",
            agent_binding=PROJECT_BINDING,
        )
        self.original_catalog_path = binding.DEFAULT_CATALOG_PATH
        self.catalog = root / "agent_bindings.json"
        self.catalog.write_text(
            json.dumps(
                {
                    "version": 1,
                    "agents": [
                        {
                            "id": self.repository.repository_id,
                            "repository": self.repository.repository,
                            "agent_binding": PROJECT_BINDING,
                            "execution_enabled": True,
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        binding.DEFAULT_CATALOG_PATH = self.catalog
        (self.repository.control / ".git").mkdir(parents=True)
        (self.repository.work / ".git").mkdir(parents=True)
        binding_path = self.repository.control / ".agent" / "binding.json"
        binding_path.parent.mkdir(parents=True)
        binding_path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "repository_id": self.repository.repository_id,
                    "repository": self.repository.repository,
                    "agent_binding": PROJECT_BINDING,
                }
            ),
            encoding="utf-8",
        )
        self.originals = {
            "STATE_DIR": agentd.STATE_DIR,
            "CONTROL": core.CONTROL,
            "WORK": core.WORK,
            "CHECKPOINTS": core.CHECKPOINTS,
            "CONTROL_BRANCH": core.CONTROL_BRANCH,
            "CLAIMS_DIR": agentd.CLAIMS_DIR,
            "CORRUPT_CLAIMS_DIR": agentd.CORRUPT_CLAIMS_DIR,
            "LOCAL_STATUS_PATH": agentd.LOCAL_STATUS_PATH,
            "LOCAL_RUNS_DIR": agentd.LOCAL_RUNS_DIR,
            "RESULT_SPOOL_DIR": agentd.RESULT_SPOOL_DIR,
        }
        agentd.STATE_DIR = root / "state"

    def tearDown(self) -> None:
        core.CONTROL = self.originals["CONTROL"]
        core.WORK = self.originals["WORK"]
        core.CHECKPOINTS = self.originals["CHECKPOINTS"]
        core.CONTROL_BRANCH = self.originals["CONTROL_BRANCH"]
        agentd.CLAIMS_DIR = self.originals["CLAIMS_DIR"]
        agentd.CORRUPT_CLAIMS_DIR = self.originals["CORRUPT_CLAIMS_DIR"]
        agentd.LOCAL_STATUS_PATH = self.originals["LOCAL_STATUS_PATH"]
        agentd.LOCAL_RUNS_DIR = self.originals["LOCAL_RUNS_DIR"]
        agentd.RESULT_SPOOL_DIR = self.originals["RESULT_SPOOL_DIR"]
        agentd.STATE_DIR = self.originals["STATE_DIR"]
        binding.DEFAULT_CATALOG_PATH = self.original_catalog_path
        self.tmp.cleanup()

    def test_bind_repository_scopes_paths_inside_worker_process(self) -> None:
        worker.bind_repository(self.repository)
        self.assertEqual(core.CONTROL, self.repository.control)
        self.assertEqual(core.WORK, self.repository.work)
        self.assertEqual(core.CHECKPOINTS, self.repository.checkpoints)
        self.assertEqual(core.CONTROL_BRANCH, "agent-control")
        state = worker.repository_state_dir(self.repository)
        self.assertEqual(agentd.CLAIMS_DIR, state / "claims")
        self.assertEqual(agentd.LOCAL_RUNS_DIR, state / "runs")
        self.assertEqual(agentd.RESULT_SPOOL_DIR, state / "result-spool")

    def test_missing_checkout_is_rejected_before_git_mutation(self) -> None:
        missing = RepositoryContext(
            repository_id="missing",
            repository="owner/missing",
            control=Path(self.tmp.name) / "missing-control",
            work=Path(self.tmp.name) / "missing-work",
            checkpoints=Path(self.tmp.name) / "missing-checkpoints",
        )
        with self.assertRaisesRegex(RuntimeError, "checkout missing"):
            worker.validate_repository_checkouts(missing)

    def test_repository_lookup_rejects_changed_configuration_digest(self) -> None:
        with mock.patch.object(
            worker,
            "load_repository_registry",
            return_value=[self.repository],
        ):
            with self.assertRaisesRegex(ValueError, "configuration changed"):
                worker.repository_by_id(
                    self.repository.repository_id,
                    registry_path=None,
                    expected_config_digest="0" * 64,
                )
            selected = worker.repository_by_id(
                self.repository.repository_id,
                registry_path=None,
                expected_config_digest=repository_config_digest(self.repository),
            )
        self.assertEqual(selected, self.repository)

    def test_remote_status_matching_fresh_payload_does_not_publish(self) -> None:
        now = datetime(2026, 9, 2, 20, 0, tzinfo=timezone.utc)
        current = {
            "state": "idle",
            "daemon_version": "4.13.1",
            "self_revision": "abc123",
        }
        remote = {**current, "updated_at": now.isoformat()}
        self.assertFalse(worker.repository_remote_status_due(current, remote, now=now))

    def test_remote_status_version_mismatch_forces_refresh(self) -> None:
        now = datetime(2026, 9, 2, 20, 0, tzinfo=timezone.utc)
        current = {
            "state": "idle",
            "daemon_version": "4.13.1",
            "self_revision": "new",
        }
        remote = {
            **current,
            "daemon_version": "4.12.2",
            "updated_at": now.isoformat(),
        }
        self.assertTrue(worker.repository_remote_status_due(current, remote, now=now))

    def test_remote_status_heartbeat_uses_remote_timestamp(self) -> None:
        now = datetime(2026, 9, 2, 20, 0, tzinfo=timezone.utc)
        current = {
            "state": "idle",
            "daemon_version": "4.13.1",
            "self_revision": "abc123",
        }
        remote = {
            **current,
            "updated_at": (
                now - timedelta(seconds=agentd.REMOTE_HEARTBEAT_SECONDS + 1)
            ).isoformat(),
        }
        self.assertTrue(worker.repository_remote_status_due(current, remote, now=now))

    def test_quiet_sync_uses_bounded_storage_policy(self) -> None:
        with mock.patch.object(storage, "sync_control") as sync:
            worker.sync_control_quietly()
        sync.assert_called_once_with(core, tasks_dir=agentd.REMOTE_TASKS_DIR)

    def test_worker_ignores_supervisor_only_self_update_without_ack(self) -> None:
        worker.bind_repository(self.repository)
        request = self.repository.control / agentd.REMOTE_CONTROL_REQUEST
        request.parent.mkdir(parents=True, exist_ok=True)
        request.write_text(
            json.dumps({"id": "self-update-test", "action": "self_update"}),
            encoding="utf-8",
        )
        with mock.patch.object(worker, "publish_repository_control_ack") as publish_ack:
            worker.handle_repository_control(self.repository)
        publish_ack.assert_not_called()

    def test_worker_ignores_supervisor_only_restart_without_ack(self) -> None:
        worker.bind_repository(self.repository)
        request = self.repository.control / agentd.REMOTE_CONTROL_REQUEST
        request.parent.mkdir(parents=True, exist_ok=True)
        request.write_text(
            json.dumps({"id": "restart-test", "action": "restart"}),
            encoding="utf-8",
        )
        with mock.patch.object(worker, "publish_repository_control_ack") as publish_ack:
            worker.handle_repository_control(self.repository)
        publish_ack.assert_not_called()

    def test_idle_poll_executes_no_task(self) -> None:
        with mock.patch.object(worker, "sync_control_quietly"), mock.patch.object(
            agentd, "recover_stale_claims"
        ), mock.patch.object(agentd, "recover_invalid_task_files"), mock.patch.object(
            agentd, "pending_tasks", return_value=[]
        ), mock.patch.object(agentd, "execute_task") as execute_task:
            processed = worker.poll_repository_once(self.repository)
        self.assertFalse(processed)
        execute_task.assert_not_called()

    def test_poll_executes_at_most_first_task(self) -> None:
        tasks = [
            (
                Path("one.json"),
                {"id": "one", "agent_binding": PROJECT_BINDING, "commands": ["true"]},
            ),
            (
                Path("two.json"),
                {"id": "two", "agent_binding": PROJECT_BINDING, "commands": ["true"]},
            ),
        ]
        with mock.patch.object(worker, "sync_control_quietly"), mock.patch.object(
            agentd, "recover_stale_claims"
        ), mock.patch.object(agentd, "recover_invalid_task_files"), mock.patch.object(
            agentd, "pending_tasks", return_value=tasks
        ), mock.patch.object(agentd, "execute_task") as execute_task:
            processed = worker.poll_repository_once(self.repository)
        self.assertTrue(processed)
        execute_task.assert_called_once_with(
            tasks[0][1],
            remote_daemon_status=False,
            remote_result_published=False,
        )

    def test_publication_pending_is_not_overwritten_with_idle(self) -> None:
        tasks = [
            (
                Path("one.json"),
                {"id": "one", "agent_binding": PROJECT_BINDING, "commands": ["true"]},
            )
        ]
        with mock.patch.object(worker, "sync_control_quietly"), mock.patch.object(
            agentd, "recover_stale_claims"
        ), mock.patch.object(agentd, "recover_invalid_task_files"), mock.patch.object(
            agentd, "pending_tasks", return_value=tasks
        ), mock.patch.object(
            agentd, "execute_task", return_value="publication_pending"
        ), mock.patch.object(worker, "publish_repository_status") as publish_status:
            processed = worker.poll_repository_once(self.repository)
        self.assertTrue(processed)
        publish_status.assert_called_once_with(
            self.repository,
            "publication_pending",
            force_remote=True,
            last_task_id="one",
        )


if __name__ == "__main__":
    unittest.main()