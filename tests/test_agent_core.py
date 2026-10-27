from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import local_agent.foundation.core as core


class WorkspaceCheckpointTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.repo = root / "repo"
        self.checkpoints = root / "checkpoints"
        self.repo.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=self.repo, check=True)
        subprocess.run(["git", "config", "user.name", "Checkpoint Test"], cwd=self.repo, check=True)
        subprocess.run(["git", "config", "user.email", "checkpoint@example.invalid"], cwd=self.repo, check=True)
        (self.repo / "tracked.txt").write_text("original\n", encoding="utf-8")
        subprocess.run(["git", "add", "tracked.txt"], cwd=self.repo, check=True)
        subprocess.run(["git", "commit", "-qm", "base"], cwd=self.repo, check=True)
        self.original_work = core.WORK
        self.original_checkpoints = core.CHECKPOINTS
        core.WORK = self.repo
        core.CHECKPOINTS = self.checkpoints

    def tearDown(self) -> None:
        core.WORK = self.original_work
        core.CHECKPOINTS = self.original_checkpoints
        self.tmp.cleanup()

    def test_single_repo_sync_uses_storage_policy(self) -> None:
        with mock.patch.object(core.storage, "sync_control") as sync:
            core.sync_control()
        sync.assert_called_once_with(core)

    def test_publish_result_uses_bounded_retry_and_scoped_commit(self) -> None:
        original_control = core.CONTROL
        core.CONTROL = self.repo
        try:
            with mock.patch.object(core, "process", return_value={"exit_code": 0, "output": ""}) as process, mock.patch.object(core.storage, "run_git_with_network_retry", return_value={"exit_code": 0, "output": ""}) as retry:
                core.publish_result("result-test", {"status": "done"})
            self.assertEqual(retry.call_count, 2)
            self.assertEqual(retry.call_args_list[0].args[1], ["git", *core.storage.bounded_control_pull_args(core.CONTROL_BRANCH)])
            self.assertEqual(retry.call_args_list[1].args[1], ["git", "push", "origin", core.CONTROL_BRANCH])
            commit_calls=[c for c in process.call_args_list if len(c.args[0]) > 1 and c.args[0][1] == "commit"]
            self.assertEqual(len(commit_calls), 1)
            self.assertEqual(commit_calls[0].args[0][-2:], ["--", ".agent/results/result-test.json"])
            self.assertTrue(all(c.kwargs.get("log_commands") is False for c in process.call_args_list))
            self.assertTrue(all(c.kwargs.get("log_commands") is False for c in retry.call_args_list))
        finally:
            core.CONTROL = original_control

    def test_publish_control_json_is_path_scoped_and_rejects_escape(self) -> None:
        original_control = core.CONTROL
        core.CONTROL = self.repo
        try:
            with mock.patch.object(
                core, "process", return_value={"exit_code": 0, "output": ""}
            ) as process, mock.patch.object(
                core.storage,
                "run_git_with_network_retry",
                return_value={"exit_code": 0, "output": ""},
            ):
                core.publish_control_json(
                    ".agent/conversation/results/operator-1.json",
                    {"state": "completed"},
                    commit_message="Conversation result: operator-1",
                )
            commit_calls = [
                call
                for call in process.call_args_list
                if len(call.args[0]) > 1 and call.args[0][1] == "commit"
            ]
            self.assertEqual(len(commit_calls), 1)
            self.assertEqual(
                commit_calls[0].args[0][-2:],
                ["--", ".agent/conversation/results/operator-1.json"],
            )
            with self.assertRaisesRegex(ValueError, "below .agent"):
                core.publish_control_json(
                    "../escape.json",
                    {},
                    commit_message="invalid",
                )
        finally:
            core.CONTROL = original_control

    def test_clean_worktree_does_not_create_checkpoint(self) -> None:
        self.assertIsNone(core.checkpoint_worktree("task-clean", reason="unit"))
        self.assertFalse(self.checkpoints.exists())

    def test_checkpoint_preserves_tracked_and_untracked_content(self) -> None:
        (self.repo / "tracked.txt").write_text("changed\n", encoding="utf-8")
        nested = self.repo / "new" / "blob.bin"
        nested.parent.mkdir()
        nested.write_bytes(b"\x00\x01checkpoint\xff")

        metadata = core.checkpoint_worktree("task-dirty", reason="unit")
        self.assertIsNotNone(metadata)
        assert metadata is not None
        checkpoint = Path(metadata["path"])
        self.assertTrue(checkpoint.is_dir())
        self.assertIn("tracked.txt", (checkpoint / "tracked.patch").read_text(encoding="utf-8"))
        self.assertEqual((checkpoint / "untracked" / "new" / "blob.bin").read_bytes(), nested.read_bytes())
        saved = json.loads((checkpoint / "metadata.json").read_text(encoding="utf-8"))
        self.assertEqual(saved["base_head"], metadata["base_head"])
        self.assertEqual(saved["untracked_files"], ["new/blob.bin"])

        subprocess.run(["git", "reset", "--hard", "-q", "HEAD"], cwd=self.repo, check=True)
        subprocess.run(["git", "clean", "-fdq"], cwd=self.repo, check=True)
        subprocess.run(["git", "apply", "--binary", str(checkpoint / "tracked.patch")], cwd=self.repo, check=True)
        restored = self.repo / "new" / "blob.bin"
        restored.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(checkpoint / "untracked" / "new" / "blob.bin", restored)
        self.assertEqual((self.repo / "tracked.txt").read_text(encoding="utf-8"), "changed\n")
        self.assertEqual(restored.read_bytes(), b"\x00\x01checkpoint\xff")

    def test_checkpoint_preflight_limit_preserves_source_and_creates_no_final_checkpoint(self) -> None:
        source = self.repo / "untracked.txt"
        source.write_text("preserve me", encoding="utf-8")
        with mock.patch.object(core, "CHECKPOINT_MAX_FILES", 0):
            with self.assertRaisesRegex(RuntimeError, "limit is 0"):
                core.checkpoint_worktree("task-limited", reason="unit")
        self.assertEqual(source.read_text(encoding="utf-8"), "preserve me")
        self.assertFalse(self.checkpoints.exists())

    def test_multiline_command_log_is_compact_by_default(self) -> None:
        newline = chr(10)
        command = newline.join(["set -e", "python - <<'PY'", "print('hello')", "PY"])
        with mock.patch.dict(core.os.environ, {}, clear=True):
            self.assertEqual(
                core.command_log_descriptor(command, {"name": "example-stage"}),
                f"stage=example-stage lines=4 chars={len(command)}",
            )

    def test_verbose_command_log_preserves_full_command(self) -> None:
        command = chr(10).join(["line one", "line two"])
        with mock.patch.dict(
            core.os.environ,
            {core.VERBOSE_COMMAND_LOG_ENV: "1"},
            clear=False,
        ):
            self.assertEqual(core.command_log_descriptor(command), command)

    def test_successful_internal_git_process_is_quiet_by_default(self) -> None:
        with mock.patch.dict(core.os.environ, {}, clear=True), mock.patch.object(
            core, "log"
        ) as logger:
            result = core.process(["git", "status", "--short"], self.repo, timeout=10)
        self.assertEqual(result["exit_code"], 0)
        logger.assert_not_called()

    def test_internal_process_output_is_strictly_bounded(self) -> None:
        result = core.process(
            ["python", "-c", "print('x' * 100000)"],
            self.repo,
            timeout=10,
            output_limit=1024,
        )
        self.assertEqual(result["exit_code"], 0)
        self.assertTrue(result["output_truncated"])
        self.assertLessEqual(len(result["output"]), 1024)

    def test_identical_commands_are_executed_each_time(self) -> None:
        calls: list[str] = []

        def runner(command: str, _timeout: int, *, stage=None):
            calls.append(command)
            return {
                "command": command,
                "exit_code": 0,
                "output": "",
                "elapsed_seconds": 0.1,
            }

        results, _history = core.run_command_list(
            ["same", "same"],
            10,
            runner=runner,
        )
        self.assertEqual(calls, ["same", "same"])
        self.assertEqual(len(results), 2)
        self.assertTrue(all("reused" not in result for result in results))


if __name__ == "__main__":
    unittest.main()
