from __future__ import annotations

import argparse
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import local_agent.cli.diagnostics as agentctl
from local_agent.repository.context import RepositoryContext


class AgentCtlStatusTests(unittest.TestCase):
    def test_pid_validation_and_permission_semantics(self) -> None:
        for value in (None, True, 0, 1, "2"):
            with self.subTest(value=value):
                self.assertFalse(agentctl._pid_alive(value))
        with mock.patch.object(agentctl.os, "kill", side_effect=ProcessLookupError):
            self.assertFalse(agentctl._pid_alive(123))
        with mock.patch.object(agentctl.os, "kill", side_effect=PermissionError):
            self.assertTrue(agentctl._pid_alive(123))
        with mock.patch.object(agentctl.os, "kill", return_value=None) as kill:
            self.assertTrue(agentctl._pid_alive(123))
        kill.assert_called_once_with(123, 0)

    def test_status_owner_prefers_entrypoint_then_supervisor_then_pid(self) -> None:
        self.assertEqual(
            agentctl._status_owner_pid(
                {"entrypoint_pid": 10, "supervisor_pid": 11, "pid": 12}
            ),
            10,
        )
        self.assertEqual(agentctl._status_owner_pid({"supervisor_pid": 11, "pid": 12}), 11)
        self.assertEqual(agentctl._status_owner_pid({"pid": 12}), 12)
        self.assertIsNone(agentctl._status_owner_pid({"pid": True}))

    def test_status_command_reports_missing_file_and_live_payload(self) -> None:
        args = argparse.Namespace()
        with mock.patch.object(agentctl, "read_json", return_value=None), mock.patch.object(
            agentctl,
            "print_json",
        ) as output:
            self.assertEqual(agentctl.command_status(args), 1)
        output.assert_called_once_with({"state": "unknown", "error": "status file not found"})

        payload = {"state": "idle", "pid": 99}
        with mock.patch.object(agentctl, "read_json", return_value=payload), mock.patch.object(
            agentctl,
            "_status_with_liveness",
            return_value={"state": "idle", "process_alive": True},
        ), mock.patch.object(agentctl, "print_json") as output:
            self.assertEqual(agentctl.command_status(args), 0)
        output.assert_called_once_with({"state": "idle", "process_alive": True})


class AgentCtlStorageTests(unittest.TestCase):
    def test_bounded_tree_stats_handles_missing_and_file_limit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            missing = root / "missing"
            self.assertEqual(agentctl._bounded_tree_stats(missing), (0, 0, False))

            tree = root / "tree"
            tree.mkdir()
            (tree / "a").write_bytes(b"123")
            (tree / "b").write_bytes(b"4567")
            with mock.patch.object(agentctl.storage, "DIAGNOSTIC_FILE_LIMIT", 100):
                self.assertEqual(agentctl._bounded_tree_stats(tree), (2, 7, False))
            with mock.patch.object(agentctl.storage, "DIAGNOSTIC_FILE_LIMIT", 1):
                files, size, truncated = agentctl._bounded_tree_stats(tree)
            self.assertEqual(files, 2)
            self.assertTrue(truncated)
            self.assertGreaterEqual(size, 0)

    def test_workspace_storage_flags_only_threshold_or_truncation(self) -> None:
        repository = RepositoryContext(
            repository_id="demo",
            repository="owner/demo",
            control=Path("/tmp/control"),
            work=Path("/tmp/work"),
            checkpoints=Path("/tmp/checkpoints"),
        )
        with mock.patch.object(
            agentctl,
            "_bounded_tree_stats",
            side_effect=[(2, 10, False), (3, 999, True)],
        ), mock.patch.object(agentctl.storage, "CONTROL_WORKTREE_WARNING_BYTES", 100), mock.patch.object(
            agentctl.storage,
            "WORKTREE_WARNING_BYTES",
            1000,
        ):
            result = agentctl._workspace_storage(repository)
        self.assertFalse(result[0]["warning"])
        self.assertTrue(result[1]["warning"])
        self.assertEqual(result[0]["kind"], "control")
        self.assertEqual(result[1]["kind"], "work")

    def test_control_history_surfaces_storage_policy_drift(self) -> None:
        repository = RepositoryContext(
            repository_id="demo",
            repository="owner/demo",
            control=Path("/tmp/control"),
            work=Path("/tmp/work"),
            checkpoints=Path("/tmp/checkpoints"),
        )
        with mock.patch.object(
            agentctl.core,
            "process",
            return_value={"exit_code": 0, "output": "7\n"},
        ), mock.patch.object(
            agentctl,
            "_bounded_tree_stats",
            return_value=(10, 1234, False),
        ), mock.patch.object(
            agentctl.storage,
            "git_bool",
            side_effect=[True, True, True],
        ), mock.patch.object(agentctl.storage, "CONTROL_HISTORY_WARNING_COMMITS", 100), mock.patch.object(
            agentctl.storage,
            "CONTROL_HISTORY_WARNING_BYTES",
            5000,
        ):
            healthy = agentctl._control_history(repository)
        self.assertFalse(healthy["warning"])
        self.assertEqual(healthy["commits"], 7)

        with mock.patch.object(
            agentctl.core,
            "process",
            return_value={"exit_code": 0, "output": "101\n"},
        ), mock.patch.object(
            agentctl,
            "_bounded_tree_stats",
            return_value=(10, 1234, False),
        ), mock.patch.object(
            agentctl.storage,
            "git_bool",
            side_effect=[False, True, True],
        ), mock.patch.object(agentctl.storage, "CONTROL_HISTORY_WARNING_COMMITS", 100), mock.patch.object(
            agentctl.storage,
            "CONTROL_HISTORY_WARNING_BYTES",
            5000,
        ):
            drift = agentctl._control_history(repository)
        self.assertTrue(drift["warning"])
        self.assertFalse(drift["shallow"])


class AgentCtlCommandTests(unittest.TestCase):
    def test_task_command_reports_unknown_single_and_multiple_matches(self) -> None:
        args = argparse.Namespace(task_id="task-a", repository_id=None)
        repository = RepositoryContext(
            repository_id="demo",
            repository="owner/demo",
            control=Path("/tmp/control"),
            work=Path("/tmp/work"),
            checkpoints=Path("/tmp/checkpoints"),
        )
        with mock.patch.object(agentctl, "load_repository_registry", return_value=[repository]), mock.patch.object(
            agentctl,
            "read_json",
            return_value=None,
        ), mock.patch.object(agentctl, "print_json") as output:
            self.assertEqual(agentctl.command_task(args), 1)
        output.assert_called_once_with({"task_id": "task-a", "state": "unknown"})

        with mock.patch.object(agentctl, "load_repository_registry", return_value=[]), mock.patch.object(
            agentctl,
            "read_json",
            return_value={"id": "task-a", "state": "running"},
        ), mock.patch.object(agentctl, "print_json") as output:
            self.assertEqual(agentctl.command_task(args), 0)
        output.assert_called_once_with({"id": "task-a", "state": "running"})

        values = iter(({"source": "legacy"}, {"source": "demo"}))
        with mock.patch.object(agentctl, "load_repository_registry", return_value=[repository]), mock.patch.object(
            agentctl,
            "read_json",
            side_effect=lambda _path: next(values),
        ), mock.patch.object(agentctl, "print_json") as output:
            self.assertEqual(agentctl.command_task(args), 0)
        output.assert_called_once_with({"matches": [{"source": "legacy"}, {"source": "demo"}]})

    def test_task_command_finds_host_mailbox_run_in_namespaced_state(self) -> None:
        host = RepositoryContext(
            repository_id="demo", repository="owner/demo",
            control=Path("/tmp/control"), work=Path("/tmp/work"),
            checkpoints=Path("/tmp/checkpoints"),
            host_id="worker-a", host_mailbox_enabled=True,
        )
        args = argparse.Namespace(task_id="host-task", repository_id="demo")
        expected = (agentctl.agentd.STATE_DIR / "repositories" / "demo"
                    / "hosts" / "worker-a" / "runs" / "host-task.json")
        visited: list[Path] = []

        def read(path: Path):
            visited.append(path)
            return {"task_id": "host-task", "state": "done"} if path == expected else None

        with mock.patch.object(agentctl, "load_repository_registry", return_value=[host]), \
             mock.patch.object(agentctl, "read_json", side_effect=read), \
             mock.patch.object(agentctl, "print_json") as output:
            self.assertEqual(agentctl.command_task(args), 0)
        self.assertIn(expected, visited)
        output.assert_called_once_with({"task_id": "host-task", "state": "done"})

    def test_validate_command_reports_loader_failure_and_computed_limits(self) -> None:
        args = argparse.Namespace(path="/tmp/task.json")
        with mock.patch.object(agentctl.agentd, "load_task_file", side_effect=ValueError("bad task")), mock.patch.object(
            agentctl,
            "print_json",
        ) as output:
            self.assertEqual(agentctl.command_validate(args), 1)
        self.assertIn("ValueError: bad task", output.call_args.args[0]["error"])

        task = {"id": "task-a"}
        with mock.patch.object(agentctl.agentd, "load_task_file", return_value=task), mock.patch.object(
            agentctl.agentd,
            "task_digest",
            return_value="digest",
        ), mock.patch.object(agentctl.core, "command_timeout_for", return_value=10), mock.patch.object(
            agentctl,
            "idle_timeout_for",
            return_value=20,
        ), mock.patch.object(agentctl, "task_timeout_for", return_value=30), mock.patch.object(
            agentctl,
            "print_json",
        ) as output:
            self.assertEqual(agentctl.command_validate(args), 0)
        self.assertEqual(
            output.call_args.args[0],
            {
                "valid": True,
                "id": "task-a",
                "task_digest": "digest",
                "command_timeout": 10,
                "idle_timeout": 20,
                "task_timeout": 30,
            },
        )

    def test_parser_exposes_all_diagnostic_commands(self) -> None:
        parser = agentctl.build_parser()
        self.assertEqual(parser.parse_args(["status"]).command, "status")
        self.assertEqual(parser.parse_args(["task", "abc"]).task_id, "abc")
        self.assertEqual(parser.parse_args(["validate-task", "task.json"]).path, "task.json")
        self.assertEqual(parser.parse_args(["doctor"]).command, "doctor")


if __name__ == "__main__":
    unittest.main()
