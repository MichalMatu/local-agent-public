from __future__ import annotations

import json
import tempfile
import threading
from contextlib import nullcontext
import unittest
from pathlib import Path
from unittest import mock

import local_agent.daemon.service as agentd


class AgentDaemonSafetyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.originals = {
            "STATE_DIR": agentd.STATE_DIR,
            "CLAIMS_DIR": agentd.CLAIMS_DIR,
            "CORRUPT_CLAIMS_DIR": agentd.CORRUPT_CLAIMS_DIR,
            "DAEMON_LOCK_PATH": agentd.DAEMON_LOCK_PATH,
            "REJECTED_UPDATE_PATH": agentd.REJECTED_UPDATE_PATH,
            "LOCAL_STATUS_PATH": agentd.LOCAL_STATUS_PATH,
            "LOCAL_RUNS_DIR": agentd.LOCAL_RUNS_DIR,
            "RESULT_SPOOL_DIR": agentd.RESULT_SPOOL_DIR,
            "CONTROL": agentd.core.CONTROL,
        }
        agentd.STATE_DIR = root / "state"
        agentd.CLAIMS_DIR = agentd.STATE_DIR / "claims"
        agentd.CORRUPT_CLAIMS_DIR = agentd.STATE_DIR / "corrupt-claims"
        agentd.DAEMON_LOCK_PATH = agentd.STATE_DIR / "agentd.lock"
        agentd.REJECTED_UPDATE_PATH = agentd.STATE_DIR / "rejected-self-update.json"
        agentd.LOCAL_STATUS_PATH = agentd.STATE_DIR / "status.json"
        agentd.LOCAL_RUNS_DIR = agentd.STATE_DIR / "runs"
        agentd.RESULT_SPOOL_DIR = agentd.STATE_DIR / "result-spool"
        agentd.core.CONTROL = root / "control"
        (agentd.core.CONTROL / ".agent" / "tasks").mkdir(parents=True)
        (agentd.core.CONTROL / ".agent" / "results").mkdir(parents=True)

    def tearDown(self) -> None:
        for key, value in self.originals.items():
            if key == "CONTROL":
                agentd.core.CONTROL = value
            else:
                setattr(agentd, key, value)
        self.tmp.cleanup()

    def task(self, task_id: str = "task-1") -> dict:
        return {"id": task_id, "mode": "commands", "resources": [], "commands": ["true"]}

    def test_publish_control_json_keeps_successful_git_plumbing_quiet(self) -> None:
        process = mock.Mock(side_effect=[
            {"exit_code": 0, "output": ""},
            {"exit_code": 1, "output": ""},
            {"exit_code": 0, "output": ""},
        ])
        retry = mock.Mock(side_effect=[
            {"exit_code": 0, "output": ""},
            {"exit_code": 0, "output": ""},
        ])
        with mock.patch.object(agentd.core, "process", process), mock.patch.object(
            agentd.storage,
            "run_git_with_network_retry",
            retry,
        ), mock.patch.object(
            agentd,
            "termination_critical_section",
            return_value=nullcontext(),
        ):
            published = agentd.publish_control_json(
                ".agent/status/daemon.json",
                {"state": "idle"},
                commit_message="Agent daemon status: idle",
            )
        self.assertTrue(published)
        self.assertTrue(process.call_args_list)
        self.assertTrue(
            all(call.kwargs.get("log_commands") is False for call in process.call_args_list)
        )
        self.assertEqual(retry.call_count, 2)
        self.assertTrue(
            all(call.kwargs.get("log_commands") is False for call in retry.call_args_list)
        )

    def test_publish_control_json_ensure_remote_pushes_even_when_nothing_is_staged(self) -> None:
        process = mock.Mock(
            side_effect=[
                {"exit_code": 0, "output": ""},
                {"exit_code": 0, "output": ""},
            ]
        )
        retry = mock.Mock(
            side_effect=[
                {"exit_code": 0, "output": ""},
                {"exit_code": 0, "output": ""},
            ]
        )
        validator = mock.Mock()
        with mock.patch.object(agentd.core, "process", process), mock.patch.object(
            agentd.storage, "run_git_with_network_retry", retry
        ), mock.patch.object(
            agentd, "termination_critical_section", return_value=nullcontext()
        ):
            published = agentd.publish_control_json(
                ".agent/status/daemon.json",
                {"state": "idle"},
                commit_message="Agent daemon status: idle",
                ensure_remote=True,
                post_pull_validate=validator,
            )
        self.assertTrue(published)
        self.assertEqual(retry.call_count, 2)
        validator.assert_called_once_with()

    def test_quiet_control_git_failure_keeps_diagnostic(self) -> None:
        failure = {
            "exit_code": 124,
            "output": "",
            "timed_out": True,
            "elapsed_seconds": 30.0,
        }
        with mock.patch.object(agentd.core, "process", return_value=failure), mock.patch.object(
            agentd,
            "termination_critical_section",
            return_value=nullcontext(),
        ):
            with self.assertRaisesRegex(RuntimeError, "timed_out=true"):
                agentd.publish_control_json(
                    ".agent/status/daemon.json",
                    {"state": "idle"},
                    commit_message="Agent daemon status: idle",
                )

    def test_daemon_status_reports_hardened_watchdog_defaults(self) -> None:
        with mock.patch.object(agentd, "self_revision", return_value="abc"):
            payload = agentd.daemon_status_payload("idle")
        self.assertEqual(payload["daemon_version"], agentd.DAEMON_VERSION)
        self.assertEqual(payload["command_timeout_default"], agentd.TIMEOUTS.command_default)
        self.assertEqual(payload["command_timeout_max"], agentd.TIMEOUTS.command_max)
        self.assertEqual(payload["idle_timeout_default"], agentd.TIMEOUTS.idle_default)
        self.assertEqual(payload["idle_timeout_max"], agentd.TIMEOUTS.idle_max)
        self.assertEqual(payload["task_timeout_default"], agentd.TIMEOUTS.task_default)
        self.assertEqual(payload["task_timeout_max"], agentd.TIMEOUTS.task_max)
        self.assertEqual(payload["memory_limit_mb_default"], 4096)

    def test_daemon_status_can_be_persisted_without_remote_git(self) -> None:
        with mock.patch.object(agentd, "self_revision", return_value="abc"), mock.patch.object(agentd, "publish_control_json") as publish:
            agentd.publish_daemon_status("running", force_remote=True, remote_enabled=False)
        publish.assert_not_called()
        payload = json.loads(agentd.LOCAL_STATUS_PATH.read_text(encoding="utf-8"))
        self.assertEqual(payload["state"], "running")

    def test_claim_blocks_duplicate_execution_and_records_digest(self) -> None:
        task = self.task()
        claim = agentd.claim_task(task)
        self.assertIsNotNone(claim)
        assert claim is not None
        self.assertEqual(claim["task_digest"], agentd.task_digest(task))
        self.assertIsNone(agentd.claim_task(task))
        agentd.release_task_claim("task-1")
        self.assertIsNotNone(agentd.claim_task(task))

    def test_claim_refuses_same_id_with_changed_payload(self) -> None:
        first = self.task()
        second = self.task()
        second["commands"] = ["false"]
        self.assertIsNotNone(agentd.claim_task(first))
        self.assertIsNone(agentd.claim_task(second))

    def test_pending_queue_skips_claimed_task(self) -> None:
        task = self.task("task-2")
        path = agentd.core.CONTROL / ".agent" / "tasks" / "task-2.json"
        path.write_text(json.dumps(task), encoding="utf-8")
        self.assertEqual(len(agentd.pending_tasks()), 1)
        self.assertIsNotNone(agentd.claim_task(task))
        self.assertEqual(agentd.pending_tasks(), [])

    def test_oversized_task_file_is_terminally_rejected_before_json_load(self) -> None:
        path = agentd.core.CONTROL / ".agent" / "tasks" / "large-task.json"
        path.write_text("{}" * 20, encoding="utf-8")
        with mock.patch.object(agentd, "MAX_TASK_FILE_BYTES", 8), mock.patch.object(
            agentd.core, "publish_result"
        ) as publish_result, mock.patch.object(agentd, "publish_run_state"):
            agentd.recover_invalid_task_files()
        self.assertEqual(publish_result.call_count, 1)
        result = publish_result.call_args.args[1]
        self.assertEqual(result["failure_reason"], "invalid_task_file")
        self.assertIn("task file exceeds", result["error"])

    def test_failed_result_publication_is_retried_from_spool_without_reexecution(self) -> None:
        task = self.task("publish-retry")
        completed = {
            "id": "publish-retry",
            "status": "done",
            "finished_at": "2026-08-24T00:00:00+00:00",
        }
        with mock.patch.object(
            agentd.runtime, "process_task", return_value=dict(completed)
        ) as process_task, mock.patch.object(
            agentd.core, "publish_result", side_effect=RuntimeError("network down")
        ), mock.patch.object(agentd, "publish_daemon_status"), mock.patch.object(
            agentd, "publish_run_state"
        ):
            outcome = agentd.execute_task(task)

        self.assertEqual(outcome, "publication_pending")
        process_task.assert_called_once()
        self.assertTrue(agentd.task_claim_path("publish-retry").exists())
        spooled = agentd.read_result_spool("publish-retry")
        self.assertIsNotNone(spooled)
        assert spooled is not None
        self.assertEqual(spooled["status"], "done")

        with mock.patch.object(agentd.core, "publish_result") as publish_result, mock.patch.object(
            agentd, "publish_run_state"
        ):
            agentd.recover_stale_claims()

        publish_result.assert_called_once()
        self.assertEqual(publish_result.call_args.args[0], "publish-retry")
        self.assertFalse(agentd.task_claim_path("publish-retry").exists())
        self.assertIsNone(agentd.read_result_spool("publish-retry"))
        process_task.assert_called_once()

    def test_execute_task_can_leave_remote_status_to_repository_worker(self) -> None:
        task = self.task("local-status-only")
        completed = {"id": "local-status-only", "status": "done", "finished_at": "now"}
        with mock.patch.object(agentd.runtime, "process_task", return_value=completed), mock.patch.object(agentd, "flush_remote_progress", return_value=True), mock.patch.object(agentd, "persist_result_spool"), mock.patch.object(agentd, "publish_durable_result"), mock.patch.object(agentd, "publish_run_state") as publish_run, mock.patch.object(agentd, "publish_daemon_status") as publish_status, mock.patch.object(agentd, "release_task_claim"), mock.patch.object(agentd, "clear_current_task"):
            outcome = agentd.execute_task(task, remote_daemon_status=False, remote_result_published=False)
        self.assertEqual(outcome, "published")
        self.assertFalse(publish_run.call_args.kwargs["force_remote"])
        self.assertTrue(publish_status.call_args_list)
        self.assertTrue(all(call.kwargs.get("remote_enabled") is False for call in publish_status.call_args_list))

    def test_interrupted_attempt_is_terminal_failure(self) -> None:
        result = agentd.interrupted_result(
            "task-3",
            {
                "started_at": "2026-08-22T00:00:00+00:00",
                "task_digest": "abc",
                "attempt_id": "attempt",
            },
        )
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["failure_reason"], "interrupted_previous_attempt")
        self.assertEqual(result["task_digest"], "abc")
        self.assertIn("Automatic replay was blocked", result["error"])

    def test_corrupt_claim_is_terminal_and_never_replayed(self) -> None:
        task = self.task("task-corrupt")
        task_path = agentd.core.CONTROL / ".agent" / "tasks" / "task-corrupt.json"
        task_path.write_text(json.dumps(task), encoding="utf-8")
        claim_path = agentd.task_claim_path("task-corrupt")
        claim_path.parent.mkdir(parents=True, exist_ok=True)
        claim_path.write_text("{broken", encoding="utf-8")
        published: list[dict] = []

        def fake_publish(task_id, result):
            published.append(dict(result))
            result_path = agentd.core.CONTROL / ".agent" / "results" / f"{task_id}.json"
            result_path.write_text(json.dumps(result), encoding="utf-8")

        with mock.patch.object(agentd.core, "publish_result", side_effect=fake_publish), mock.patch.object(
            agentd, "publish_run_state"
        ) as publish_run:
            agentd.recover_stale_claims()

        self.assertEqual(len(published), 1)
        self.assertEqual(published[0]["status"], "failed")
        self.assertEqual(published[0]["failure_reason"], "corrupt_claim_state")
        self.assertIn("Automatic replay was blocked", published[0]["error"])
        self.assertFalse(claim_path.exists())
        self.assertEqual(len(list(agentd.CORRUPT_CLAIMS_DIR.glob("*.json"))), 1)
        self.assertEqual(agentd.pending_tasks(), [])
        self.assertTrue(publish_run.call_args.kwargs["force_remote"])

    def test_self_update_requires_clean_main_checkout(self) -> None:
        clean = mock.Mock(returncode=0, stdout="")
        dirty = mock.Mock(returncode=0, stdout="?? stray.py\n")
        main = mock.Mock(returncode=0, stdout="main\n")
        staging = mock.Mock(returncode=0, stdout="v4.2-staging\n")
        with mock.patch.object(agentd, "_git", return_value=clean):
            self.assertTrue(agentd.tracked_self_repo_clean())
        with mock.patch.object(agentd, "_git", return_value=dirty):
            self.assertFalse(agentd.tracked_self_repo_clean())
        with mock.patch.object(agentd, "_git", return_value=main):
            self.assertTrue(agentd.self_repo_on_main_branch())
        with mock.patch.object(agentd, "_git", return_value=staging):
            self.assertFalse(agentd.self_repo_on_main_branch())

    def test_self_update_validation_uses_isolated_home_and_all_release_modules(self) -> None:
        calls: list[tuple[list[str], dict]] = []

        def fake_process(command, _cwd, **kwargs):
            calls.append((list(command), dict(kwargs)))
            return {"exit_code": 0, "output": "", "elapsed_seconds": 0.1}

        with mock.patch.object(agentd.core, "process", side_effect=fake_process):
            valid, error = agentd._validate_installed_update()

        self.assertTrue(valid)
        self.assertEqual(error, "")
        self.assertEqual(len(calls), 2)
        compile_command, compile_kwargs = calls[0]
        self.assertEqual(
            compile_command, [agentd.sys.executable, "scripts/verify.py", "--only", "compile"]
        )
        self.assertEqual(compile_kwargs["timeout"], agentd.SELF_UPDATE_VALIDATION_TIMEOUT_SECONDS)
        self.assertEqual(calls[1][1]["timeout"], agentd.SELF_UPDATE_VALIDATION_TIMEOUT_SECONDS)
        self.assertNotEqual(compile_kwargs["environment"]["HOME"], str(agentd.HOME))
        self.assertEqual(
            compile_kwargs["environment"]["HOME"],
            calls[1][1]["environment"]["HOME"],
        )

    def test_self_update_publishes_busy_status_before_validation(self) -> None:
        local_sha = "1" * 40
        remote_sha = "2" * 40
        self_repo = Path(self.tmp.name) / "self-repo"
        (self_repo / ".git").mkdir(parents=True)
        events: list[str] = []

        def fake_git(args, timeout=60):
            del timeout
            if args == ["git", "fetch", "--quiet", "origin", agentd.SELF_BRANCH]:
                return agentd.subprocess.CompletedProcess(args, 0, "")
            if args == ["git", "rev-parse", "HEAD"]:
                return agentd.subprocess.CompletedProcess(args, 0, local_sha)
            if args == ["git", "rev-parse", f"origin/{agentd.SELF_BRANCH}"]:
                return agentd.subprocess.CompletedProcess(args, 0, remote_sha)
            if args == ["git", "merge-base", "--is-ancestor", local_sha, remote_sha]:
                return agentd.subprocess.CompletedProcess(args, 0, "")
            if args == ["git", "merge", "--ff-only", "--quiet", remote_sha]:
                return agentd.subprocess.CompletedProcess(args, 0, "")
            if args == ["git", "reset", "--hard", local_sha]:
                return agentd.subprocess.CompletedProcess(args, 0, "")
            raise AssertionError(f"unexpected git invocation: {args!r}")

        def fake_status(state, **kwargs):
            events.append("status")
            self.assertEqual(state, "self_updating")
            self.assertTrue(kwargs["force_remote"])
            self.assertEqual(kwargs["current_revision"], local_sha)
            self.assertEqual(kwargs["candidate_revision"], remote_sha)

        def fake_validate():
            events.append("validate")
            return False, "synthetic validation failure"

        with mock.patch.object(agentd, "SELF_REPO", self_repo), mock.patch.object(
            agentd, "self_repo_on_main_branch", return_value=True
        ), mock.patch.object(
            agentd, "tracked_self_repo_clean", return_value=True
        ), mock.patch.object(
            agentd, "_git", side_effect=fake_git
        ), mock.patch.object(
            agentd, "_read_rejected_update", return_value={}
        ), mock.patch.object(
            agentd, "publish_daemon_status", side_effect=fake_status
        ), mock.patch.object(
            agentd, "begin_installation"
        ), mock.patch.object(
            agentd, "_validate_installed_update", side_effect=fake_validate
        ), mock.patch.object(
            agentd, "_remember_rejected_update"
        ), mock.patch.object(
            agentd, "finish_installation"
        ):
            self.assertFalse(agentd._self_update_transaction(force=True))

        self.assertEqual(events, ["status", "validate"])

    def test_invalid_task_file_becomes_terminal_result(self) -> None:
        path = agentd.core.CONTROL / ".agent" / "tasks" / "broken-task.json"
        path.write_text("{broken", encoding="utf-8")
        published: list[dict] = []

        def fake_publish(task_id, result):
            published.append(dict(result))
            result_path = agentd.core.CONTROL / ".agent" / "results" / f"{task_id}.json"
            result_path.write_text(json.dumps(result), encoding="utf-8")

        with mock.patch.object(agentd.core, "publish_result", side_effect=fake_publish), mock.patch.object(
            agentd, "publish_run_state"
        ) as publish_run:
            agentd.recover_invalid_task_files()

        self.assertEqual(len(published), 1)
        self.assertEqual(published[0]["id"], "broken-task")
        self.assertEqual(published[0]["failure_reason"], "invalid_task_file")
        self.assertEqual(agentd.pending_tasks(), [])
        self.assertTrue(publish_run.call_args.kwargs["force_remote"])

    def test_symlinked_task_file_is_rejected_without_reading_its_target(self) -> None:
        outside = agentd.STATE_DIR / "outside-task.json"
        outside.parent.mkdir(parents=True, exist_ok=True)
        outside.write_text(json.dumps(self.task("outside")), encoding="utf-8")
        path = agentd.core.CONTROL / ".agent" / "tasks" / "linked-task.json"
        path.symlink_to(outside)
        with mock.patch.object(agentd.core, "publish_result") as publish_result, mock.patch.object(
            agentd, "publish_run_state"
        ):
            agentd.recover_invalid_task_files()
        result = publish_result.call_args.args[1]
        self.assertEqual(result["failure_reason"], "invalid_task_file")
        self.assertIn("regular file", result["error"])

    def test_historical_filename_alias_is_valid(self) -> None:
        task = self.task("payload-id")
        path = agentd.core.CONTROL / ".agent" / "tasks" / "000-payload-id.json"
        path.write_text(json.dumps(task), encoding="utf-8")

        with mock.patch.object(agentd.core, "publish_result") as publish_result, mock.patch.object(
            agentd, "publish_run_state"
        ):
            agentd.recover_invalid_task_files()
        publish_result.assert_not_called()

        pending = agentd.pending_tasks()
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0][1]["id"], "payload-id")

        result_path = agentd.core.CONTROL / ".agent" / "results" / "payload-id.json"
        result_path.write_text(
            json.dumps({"id": "payload-id", "status": "done", "task_digest": agentd.task_digest(task)}),
            encoding="utf-8",
        )
        self.assertEqual(agentd.pending_tasks(), [])

    def test_progress_callback_persists_local_state(self) -> None:
        callback = agentd.make_progress_callback("task-4", "attempt", "digest")
        with mock.patch.object(agentd, "publish_control_json", return_value=True):
            callback(
                {
                    "event": "command_started",
                    "phase": "commands",
                    "index": 1,
                    "total": 2,
                    "command": "true",
                }
            )
            self.assertTrue(agentd.flush_remote_progress())
        state = json.loads((agentd.LOCAL_RUNS_DIR / "task-4.json").read_text())
        self.assertEqual(state["task_id"], "task-4")
        self.assertEqual(state["attempt_id"], "attempt")
        self.assertEqual(state["event"], "command_started")

    def test_remote_progress_quiesce_waits_for_active_publish_and_rejects_new_work(
        self,
    ) -> None:
        started = threading.Event()
        release = threading.Event()

        def blocking_publish(*_args, **_kwargs):
            started.set()
            self.assertTrue(release.wait(timeout=2))
            return True

        with mock.patch.object(
            agentd, "publish_control_json", side_effect=blocking_publish
        ) as publish:
            publisher = agentd.CoalescingRemotePublisher()
            publisher.submit(".agent/runs/one.json", {}, "progress one")
            self.assertTrue(started.wait(timeout=2))
            self.assertFalse(publisher.quiesce(timeout=0.01))
            release.set()
            self.assertTrue(publisher.quiesce(timeout=2))
            publisher.submit(".agent/runs/two.json", {}, "progress two")
            self.assertTrue(publisher.flush(timeout=0.1))

        self.assertEqual(publish.call_count, 1)

    def test_shutdown_stops_task_before_quiescing_and_terminating_processes(
        self,
    ) -> None:
        calls: list[str] = []
        with mock.patch.object(
            agentd.runtime,
            "terminate_active_command",
            side_effect=lambda: calls.append("task"),
        ), mock.patch.object(
            agentd,
            "quiesce_remote_progress",
            side_effect=lambda: calls.append("progress") or True,
        ), mock.patch.object(
            agentd,
            "terminate_active_processes",
            side_effect=lambda _log: calls.append("remaining"),
        ):
            agentd.shutdown_runtime_processes()

        self.assertEqual(calls, ["task", "progress", "remaining"])

    def test_progress_callback_publishes_boundaries_and_status(self) -> None:
        callback = agentd.make_progress_callback("task-5", "attempt", "digest")
        with mock.patch.object(agentd, "publish_run_state") as publish_run, mock.patch.object(
            agentd, "publish_daemon_status"
        ) as publish_status, mock.patch.object(
            agentd.time, "monotonic", side_effect=[100.0, 101.0, 102.0, 170.0]
        ):
            callback({"event": "task_started"})
            callback(
                {
                    "event": "command_started",
                    "phase": "commands",
                    "index": 1,
                    "total": 3,
                    "command": "one",
                    "verification_level": "work",
                }
            )
            callback(
                {
                    "event": "command_started",
                    "phase": "commands",
                    "index": 2,
                    "total": 3,
                    "command": "two",
                }
            )
            callback(
                {
                    "event": "command_started",
                    "phase": "commands",
                    "index": 3,
                    "total": 3,
                    "command": "three",
                }
            )

        remote_flags = [call.kwargs["force_remote"] for call in publish_run.call_args_list]
        self.assertEqual(remote_flags, [True, True, False, True])
        self.assertEqual(
            [call.kwargs["force_remote"] for call in publish_status.call_args_list],
            [False, False, False, False],
        )
        self.assertEqual(
            publish_run.call_args_list[1].args[1]["verification_level"], "work"
        )
        self.assertEqual(
            publish_status.call_args_list[1].kwargs["progress"][
                "verification_level"
            ],
            "work",
        )

    def test_remote_heartbeat_refreshes_daemon_status_at_sixty_seconds(self) -> None:
        callback = agentd.make_progress_callback("task-heartbeat", "attempt", "digest")
        with mock.patch.object(agentd, "publish_run_state") as publish_run, mock.patch.object(
            agentd, "publish_daemon_status"
        ) as publish_status, mock.patch.object(
            agentd.time, "monotonic", side_effect=[100.0, 101.0, 162.0]
        ):
            callback({"event": "task_started"})
            callback(
                {
                    "event": "command_started",
                    "stage_name": "stress-run",
                    "stage_index": 1,
                    "stage_total": 1,
                    "stage_phase": "commands",
                    "command": "long-script",
                }
            )
            callback(
                {
                    "event": "command_heartbeat",
                    "stage_name": "stress-run",
                    "stage_index": 1,
                    "stage_total": 1,
                    "stage_phase": "commands",
                    "command": "long-script",
                    "host_load_1m": 1.25,
                }
            )

        self.assertTrue(publish_run.call_args_list[-1].kwargs["force_remote"])
        self.assertFalse(publish_status.call_args_list[-1].kwargs["force_remote"])
        self.assertEqual(
            publish_status.call_args_list[-1].kwargs["progress"]["host_load_1m"], 1.25
        )

    def test_marker_stage_change_publishes_remote_progress_immediately(self) -> None:
        callback = agentd.make_progress_callback("task-marker", "attempt", "digest")
        with mock.patch.object(agentd, "publish_run_state") as publish_run, mock.patch.object(
            agentd, "publish_daemon_status"
        ) as publish_status, mock.patch.object(
            agentd.time, "monotonic", side_effect=[100.0, 101.0, 102.0]
        ):
            callback({"event": "task_started"})
            callback(
                {
                    "event": "command_started",
                    "stage_name": "script",
                    "stage_index": 1,
                    "stage_total": 1,
                    "stage_phase": "commands",
                }
            )
            callback(
                {
                    "event": "stage_progress",
                    "stage_name": "case-2",
                    "stage_phase": "commands",
                    "last_progress_at": "2026-08-23T17:00:00+00:00",
                    "last_progress_message": "case 2",
                }
            )

        self.assertTrue(publish_run.call_args_list[-1].kwargs["force_remote"])
        self.assertEqual(
            publish_status.call_args_list[-1].kwargs["last_progress_message"], "case 2"
        )

    def test_failed_command_is_published_immediately(self) -> None:
        callback = agentd.make_progress_callback("task-6", "attempt", "digest")
        with mock.patch.object(agentd, "publish_run_state") as publish_run, mock.patch.object(
            agentd, "publish_daemon_status"
        ), mock.patch.object(agentd.time, "monotonic", side_effect=[100.0, 101.0]):
            callback({"event": "task_started"})
            callback(
                {
                    "event": "command_finished",
                    "phase": "commands",
                    "index": 1,
                    "total": 1,
                    "command": "false",
                    "exit_code": 1,
                    "elapsed_seconds": 0.1,
                }
            )

        self.assertTrue(publish_run.call_args_list[-1].kwargs["force_remote"])

    def test_long_successful_command_finish_is_published_immediately(self) -> None:
        callback = agentd.make_progress_callback("task-7", "attempt", "digest")
        with mock.patch.object(agentd, "publish_run_state") as publish_run, mock.patch.object(
            agentd, "publish_daemon_status"
        ), mock.patch.object(agentd.time, "monotonic", side_effect=[100.0, 101.0]):
            callback({"event": "task_started"})
            callback(
                {
                    "event": "command_finished",
                    "phase": "commands",
                    "index": 1,
                    "total": 1,
                    "command": "slow-success",
                    "exit_code": 0,
                    "elapsed_seconds": agentd.RUN_PROGRESS_SECONDS,
                }
            )

        self.assertTrue(publish_run.call_args_list[-1].kwargs["force_remote"])

    def test_control_ack_published_checks_remote_tracking_branch(self) -> None:
        relative = f"{agentd.REMOTE_CONTROL_ACK_DIR}/ack-1.json"
        with mock.patch.object(
            agentd.core,
            "process",
            return_value={"exit_code": 0, "output": relative + "\n"},
        ) as process:
            self.assertTrue(agentd.control_ack_published("ack-1"))
        self.assertEqual(
            process.call_args.args[0],
            [
                "git",
                "ls-tree",
                "--name-only",
                f"origin/{agentd.core.CONTROL_BRANCH}",
                "--",
                relative,
            ],
        )
        self.assertFalse(process.call_args.kwargs["log_commands"])

    def test_control_ack_local_only_is_not_considered_published(self) -> None:
        ack = agentd._control_ack_path("local-only")
        ack.parent.mkdir(parents=True, exist_ok=True)
        ack.write_text("{}\n", encoding="utf-8")
        with mock.patch.object(
            agentd.core,
            "process",
            return_value={"exit_code": 0, "output": ""},
        ):
            self.assertFalse(agentd.control_ack_published("local-only"))

    def test_remote_restart_control_is_acknowledged_before_restart(self) -> None:
        request_path = agentd.core.CONTROL / agentd.REMOTE_CONTROL_REQUEST
        request_path.parent.mkdir(parents=True, exist_ok=True)
        request_path.write_text(
            json.dumps({"id": "restart-1", "action": "restart"}),
            encoding="utf-8",
        )
        calls: list[str] = []

        def fake_publish(relative, payload, *, commit_message):
            calls.append(relative)
            ack = agentd.core.CONTROL / relative
            ack.parent.mkdir(parents=True, exist_ok=True)
            ack.write_text(json.dumps(payload), encoding="utf-8")
            return True

        with mock.patch.object(agentd, "control_ack_published", return_value=False), mock.patch.object(
            agentd, "publish_control_json", side_effect=fake_publish
        ), mock.patch.object(
            agentd, "restart_self", side_effect=RuntimeError("restart called")
        ):
            with self.assertRaisesRegex(RuntimeError, "restart called"):
                agentd.handle_control_request()
        self.assertIn(".agent/daemon/acks/restart-1.json", calls)

    def test_timeout_policy_uses_startup_configuration(self) -> None:
        self.assertEqual(agentd.core.COMMAND_TIMEOUT, agentd.TIMEOUTS.command_default)
        self.assertEqual(agentd.core.MAX_COMMAND_TIMEOUT, agentd.TIMEOUTS.command_max)
        self.assertEqual(agentd.runtime._idle_timeout, agentd.TIMEOUTS.idle_default)


if __name__ == "__main__":
    unittest.main()
