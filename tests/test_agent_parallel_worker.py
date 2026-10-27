from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import local_agent.supervisor.worker as worker
from local_agent.foundation.process import ExecutionLeaseBusy, RESOURCE_LEASE_FDS_ENV
from local_agent.repository.context import RepositoryContext
from local_agent.version import RELEASE_VERSION
from local_agent.runtime.task_contract import MAX_MEMORY_LIMIT_MB, MAX_TASK_RESOURCES


class ParallelWorkerResourceTests(unittest.TestCase):
    def test_parallel_daemon_version_matches_release(self) -> None:
        self.assertEqual(worker.PARALLEL_DAEMON_VERSION, RELEASE_VERSION)

    def test_resources_must_be_declared_explicitly(self) -> None:
        with self.assertRaisesRegex(ValueError, "declared explicitly"):
            worker.task_resources({"id": "a"})

    def test_empty_resources_is_software_only_independent_of_memory_limit(self) -> None:
        self.assertEqual(worker.task_resources({"id": "a", "resources": []}), ())
        self.assertEqual(
            worker.task_resources(
                {"id": "a", "resources": [], "memory_limit_mb": 0}
            ),
            (),
        )
        self.assertEqual(
            worker.task_resources(
                {
                    "id": "a",
                    "resources": [],
                    "memory_limit_mb": MAX_MEMORY_LIMIT_MB,
                }
            ),
            (),
        )

    def test_named_resources_preserve_explicit_canonical_names(self) -> None:
        task = {
            "id": "a",
            "resources": ["board:growbox-s3", "serial:s3"],
            "memory_limit_mb": 2048,
        }
        self.assertEqual(
            worker.task_resources(task),
            ("board:growbox-s3", "serial:s3"),
        )

    def test_machine_resource_must_be_declared_alone(self) -> None:
        self.assertEqual(
            worker.task_resources({"id": "a", "resources": ["machine"]}),
            ("machine",),
        )
        with self.assertRaisesRegex(ValueError, "must be declared alone"):
            worker.task_resources(
                {"id": "a", "resources": ["machine", "board:zigbee-c6"]}
            )

    def test_malformed_resources_are_rejected(self) -> None:
        invalid = (
            {"id": "a", "resources": "usb"},
            {"id": "a", "resources": ["bad value"]},
            {"id": "a", "resources": ["USB"]},
            {"id": "a", "resources": [""]},
            {"id": "a", "resources": [" usb"]},
            {"id": "a", "resources": [1]},
        )
        for task in invalid:
            with self.subTest(task=task), self.assertRaises(ValueError):
                worker.task_resources(task)

    def test_duplicate_resources_are_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "duplicate resource"):
            worker.task_resources(
                {"id": "a", "resources": ["board:s3", "board:s3"]}
            )

    def test_resource_list_is_bounded(self) -> None:
        task = {
            "id": "a",
            "resources": [f"resource-{index}" for index in range(MAX_TASK_RESOURCES + 1)],
        }
        with self.assertRaisesRegex(ValueError, "resources exceeds"):
            worker.task_resources(task)

    def test_named_resource_lease_exports_and_restores_inherited_descriptors(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(
            worker,
            "resource_lock_dir",
            return_value=Path(tmp),
        ), mock.patch.dict(
            os.environ,
            {RESOURCE_LEASE_FDS_ENV: "91"},
            clear=False,
        ):
            previous_core = worker.core.ENV.get(RESOURCE_LEASE_FDS_ENV)
            worker.core.ENV[RESOURCE_LEASE_FDS_ENV] = "core-before"
            try:
                with worker.machine_resource_lease(
                    {"id": "lease", "resources": ["board:test-s3"]}
                ) as resources:
                    self.assertEqual(resources, ("board:test-s3",))
                    inherited = os.environ[RESOURCE_LEASE_FDS_ENV].split(",")
                    self.assertEqual(inherited[0], "91")
                    self.assertGreaterEqual(len(inherited), 3)
                    self.assertEqual(
                        worker.core.ENV[RESOURCE_LEASE_FDS_ENV],
                        os.environ[RESOURCE_LEASE_FDS_ENV],
                    )
                self.assertEqual(os.environ[RESOURCE_LEASE_FDS_ENV], "91")
                self.assertEqual(worker.core.ENV[RESOURCE_LEASE_FDS_ENV], "core-before")
            finally:
                if previous_core is None:
                    worker.core.ENV.pop(RESOURCE_LEASE_FDS_ENV, None)
                else:
                    worker.core.ENV[RESOURCE_LEASE_FDS_ENV] = previous_core

    def test_waiting_status_preserves_timestamp_only_for_identical_wait(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(
            worker.agentd,
            "LOCAL_STATUS_PATH",
            Path(tmp) / "status.json",
        ), mock.patch.object(
            worker.agentd,
            "now_iso",
            return_value="2026-09-05T00:00:00+00:00",
        ):
            path = worker.agentd.LOCAL_STATUS_PATH
            self.assertEqual(
                worker._waiting_status_context("task-a", "board:a"),
                ("2026-09-05T00:00:00+00:00", True),
            )
            path.write_text(
                json.dumps(
                    {
                        "state": "waiting_resource",
                        "pending_task_id": "task-a",
                        "blocked_resources": ["board:a"],
                        "waiting_since": "2026-09-04T23:59:00+00:00",
                    }
                ),
                encoding="utf-8",
            )
            self.assertEqual(
                worker._waiting_status_context("task-a", "board:a"),
                ("2026-09-04T23:59:00+00:00", False),
            )
            self.assertEqual(
                worker._waiting_status_context("task-a", "board:b"),
                ("2026-09-05T00:00:00+00:00", True),
            )


class ParallelWorkerDedupeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.repository = RepositoryContext(
            repository_id="project-a",
            repository="owner/project-a",
            control=root / "control",
            work=root / "work",
            checkpoints=root / "checkpoints",
            agent_binding="3da0947d-9acf-4ecf-adce-a29be7dc5c09",
        )
        self.state_dir = root / "state"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def task(self, task_id: str) -> dict[str, object]:
        return {
            "id": task_id,
            "agent_binding": self.repository.agent_binding,
            "mode": "commands",
            "work_branch": "main",
            "allow_write": False,
            "resources": [],
            "commands": ["true"],
        }

    def test_coalesce_publishes_terminal_done_result_for_duplicate(self) -> None:
        first = self.task("task-a")
        second = self.task("task-b")
        pending = [(Path("a.json"), first), (Path("b.json"), second)]

        with mock.patch.object(
            worker.serial_worker,
            "repository_state_dir",
            return_value=self.state_dir,
        ), mock.patch.object(worker.core, "publish_result") as publish_result, mock.patch.object(
            worker.agentd,
            "publish_run_state",
        ) as publish_run:
            candidates = worker._coalesce_pending_tasks(self.repository, pending)

        self.assertEqual([item[1]["id"] for item in candidates], ["task-a"])
        publish_result.assert_called_once()
        result = publish_result.call_args.args[1]
        self.assertEqual(result["id"], "task-b")
        self.assertEqual(result["status"], "done")
        self.assertEqual(result["outcome"], "duplicate_task_suppressed")
        self.assertEqual(result["duplicate_of"], "task-a")
        publish_run.assert_called_once()

    def test_invalid_dedupe_key_is_terminally_rejected(self) -> None:
        task = self.task("task-a")
        task["dedupe_key"] = "bad key"

        with mock.patch.object(
            worker.serial_worker,
            "repository_state_dir",
            return_value=self.state_dir,
        ), mock.patch.object(worker.core, "publish_result") as publish_result, mock.patch.object(
            worker.agentd,
            "publish_run_state",
        ):
            candidates = worker._coalesce_pending_tasks(
                self.repository,
                [(Path("a.json"), task)],
            )

        self.assertEqual(candidates, [])
        result = publish_result.call_args.args[1]
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["failure_reason"], "invalid_dedupe_key")

    def test_conflicting_plan_publishes_explicit_failure_without_admission(self) -> None:
        first = {**self.task("task-a"), "dedupe_key": "fix"}
        second = {**self.task("task-b"), "dedupe_key": "fix", "commands": ["false"]}
        with mock.patch.object(worker.serial_worker, "repository_state_dir", return_value=self.state_dir), mock.patch.object(worker.core, "publish_result") as publish, mock.patch.object(worker.agentd, "publish_run_state") as progress:
            candidates = worker._coalesce_pending_tasks(
                self.repository, [(Path("a.json"), first), (Path("b.json"), second)],
            )
        self.assertEqual([item[1]["id"] for item in candidates], ["task-a"])
        self.assertEqual(publish.call_args.args[1]["failure_reason"], "dedupe_intent_conflict")
        self.assertEqual(progress.call_args.args[1]["failure_reason"], "dedupe_intent_conflict")

    def test_recent_receipt_is_applied_by_worker_queue_coalescing(self) -> None:
        first = self.task("task-a")
        second = self.task("task-b")
        worker.task_dedupe.record_admission(self.state_dir, first, now_epoch=100.0)
        claim_path = worker.task_dedupe._claim_path(self.state_dir, "task-a")
        claim_path.parent.mkdir(parents=True, exist_ok=True)
        claim_path.write_text("{}\n", encoding="utf-8")

        with mock.patch.object(
            worker.serial_worker,
            "repository_state_dir",
            return_value=self.state_dir,
        ), mock.patch.object(worker.task_dedupe.time, "time", return_value=101.0), mock.patch.object(
            worker.core,
            "publish_result",
        ) as publish_result, mock.patch.object(worker.agentd, "publish_run_state"):
            candidates = worker._coalesce_pending_tasks(
                self.repository,
                [(Path("b.json"), second)],
            )

        self.assertEqual(candidates, [])
        result = publish_result.call_args.args[1]
        self.assertEqual(result["status"], "done")
        self.assertEqual(result["duplicate_reason"], "recent_duplicate")
        self.assertEqual(result["duplicate_of"], "task-a")


class ParallelWorkerMainTests(unittest.TestCase):
    def setUp(self) -> None:
        root = Path("/tmp/parallel-worker-main")
        self.repository = RepositoryContext(
            repository_id="project-a",
            repository="owner/project-a",
            control=root / "control",
            work=root / "work",
            checkpoints=root / "checkpoints",
            agent_binding="3da0947d-9acf-4ecf-adce-a29be7dc5c09",
        )
        self.args = SimpleNamespace(
            repository_id=self.repository.repository_id,
            registry=None,
            expected_config_digest=None,
        )

    def run_main(self, **patches: object) -> int:
        defaults: dict[str, object] = {
            "install_signal_handlers": mock.DEFAULT,
            "parse_args": self.args,
            "repository_by_id": self.repository,
            "repository_execution_lease": mock.DEFAULT,
            "poll_repository_once": False,
        }
        defaults.update(patches)
        with mock.patch.object(worker.serial_worker, "install_signal_handlers"), mock.patch.object(
            worker.serial_worker,
            "parse_args",
            return_value=defaults["parse_args"],
        ), mock.patch.object(
            worker.serial_worker,
            "repository_by_id",
            side_effect=(
                defaults["repository_by_id"]
                if isinstance(defaults["repository_by_id"], BaseException)
                else None
            ),
            return_value=(
                None
                if isinstance(defaults["repository_by_id"], BaseException)
                else defaults["repository_by_id"]
            ),
        ), mock.patch.object(
            worker.serial_worker,
            "repository_execution_lease",
            side_effect=(
                defaults["repository_execution_lease"]
                if isinstance(defaults["repository_execution_lease"], BaseException)
                else None
            ),
        ), mock.patch.object(
            worker,
            "poll_repository_once",
            side_effect=(
                defaults["poll_repository_once"]
                if isinstance(defaults["poll_repository_once"], BaseException)
                else None
            ),
            return_value=(
                False
                if isinstance(defaults["poll_repository_once"], BaseException)
                else defaults["poll_repository_once"]
            ),
        ):
            return worker.main()

    def test_main_rejects_changed_repository_configuration(self) -> None:
        result = self.run_main(repository_by_id=ValueError("registry changed"))
        self.assertEqual(result, worker.serial_worker.WORKER_CONFIG_CHANGED)

    def test_main_maps_repository_execution_lease_contention(self) -> None:
        result = self.run_main(
            repository_execution_lease=ExecutionLeaseBusy("id:project-a")
        )
        self.assertEqual(result, worker.serial_worker.WORKER_BUSY)

    def test_main_maps_machine_and_named_resource_contention(self) -> None:
        self.assertEqual(
            self.run_main(poll_repository_once=worker.MachineResourceBusy("machine")),
            worker.WORKER_MACHINE_BUSY,
        )
        self.assertEqual(
            self.run_main(poll_repository_once=worker.MachineResourceBusy("board:test-s3")),
            worker.WORKER_RESOURCE_BUSY,
        )

    def test_main_maps_processed_and_idle_turns(self) -> None:
        self.assertEqual(
            self.run_main(poll_repository_once=True),
            worker.serial_worker.WORKER_PROCESSED,
        )
        self.assertEqual(
            self.run_main(poll_repository_once=False),
            worker.serial_worker.WORKER_IDLE,
        )


if __name__ == "__main__":
    unittest.main()
