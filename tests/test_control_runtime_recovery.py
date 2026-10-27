from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import local_agent.foundation.storage as storage
import local_agent.repository.cleanup as cleanup
from local_agent.foundation.process import LEASE_FDS_ENV, RESOURCE_LEASE_FDS_ENV


class InterruptedRuntimeTaskRecoveryTests(unittest.TestCase):
    def test_staged_owner_edit_then_worktree_delete_is_never_gc(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)

            def git(*args):
                return subprocess.run(["git", *args], cwd=repo, check=True,
                                      capture_output=True, text=True, timeout=10).stdout

            git("init", "-q")
            git("config", "user.name", "storage-test")
            git("config", "user.email", "storage@example.invalid")
            task = repo / ".agent/tasks/pending.json"
            task.parent.mkdir(parents=True)
            task.write_text('{"id":"pending"}\n')
            git("add", ".agent")
            git("commit", "-qm", "base")
            task.write_text('{"id":"pending","commands":["owner edit"]}\n')
            git("add", ".agent")
            task.unlink()
            staged = git("show", ":.agent/tasks/pending.json")
            before = git("status", "--porcelain=v1")
            self.assertTrue(before.startswith("MD "))

            def process(args, cwd, **_kwargs):
                result = subprocess.run(args, cwd=cwd, capture_output=True,
                                        text=True, timeout=10)
                return {"exit_code": result.returncode,
                        "output": result.stdout + result.stderr}

            core = SimpleNamespace(CONTROL=repo, process=process)
            with self.assertRaisesRegex(RuntimeError, "unexpected local changes"):
                storage.recover_daemon_owned_control_changes(core)
            self.assertFalse(task.exists())
            self.assertEqual(git("show", ":.agent/tasks/pending.json"), staged)
            self.assertEqual(git("status", "--porcelain=v1"), before)

    def test_interrupted_gc_task_deletion_is_restored(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
            subprocess.run(["git", "config", "user.name", "storage-test"], cwd=repo, check=True)
            subprocess.run(["git", "config", "user.email", "storage@example.invalid"], cwd=repo, check=True)
            task = repo / ".agent" / "tasks" / "terminal.json"
            task.parent.mkdir(parents=True)
            task.write_text('{"id":"terminal"}\n', encoding="utf-8")
            subprocess.run(["git", "add", ".agent/tasks/terminal.json"], cwd=repo, check=True)
            subprocess.run(["git", "commit", "-qm", "base"], cwd=repo, check=True)
            task.unlink()

            def process(args, cwd, **_kwargs):
                completed = subprocess.run(
                    args,
                    cwd=cwd,
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    check=False,
                )
                return {"exit_code": completed.returncode, "output": completed.stdout}

            core = mock.Mock()
            core.CONTROL = repo
            core.process.side_effect = process
            storage.recover_daemon_owned_control_changes(core)

            self.assertTrue(task.exists())
            self.assertEqual(task.read_text(encoding="utf-8"), '{"id":"terminal"}\n')
            status = subprocess.run(
                ["git", "status", "--porcelain=v1"],
                cwd=repo,
                text=True,
                stdout=subprocess.PIPE,
                check=True,
            )
            self.assertEqual(status.stdout, "")

    def test_interrupted_conversation_result_is_recoverable_daemon_output(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
            subprocess.run(["git", "config", "user.name", "storage-test"], cwd=repo, check=True)
            subprocess.run(["git", "config", "user.email", "storage@example.invalid"], cwd=repo, check=True)
            marker = repo / ".agent" / "status" / "base.json"
            marker.parent.mkdir(parents=True)
            marker.write_text("{}\n", encoding="utf-8")
            subprocess.run(["git", "add", ".agent/status/base.json"], cwd=repo, check=True)
            subprocess.run(["git", "commit", "-qm", "base"], cwd=repo, check=True)
            result = repo / ".agent" / "conversation" / "results" / "operator-1.json"
            result.parent.mkdir(parents=True)
            result.write_text('{"state":"completed"}\n', encoding="utf-8")

            def process(args, cwd, **_kwargs):
                completed = subprocess.run(
                    args, cwd=cwd, text=True, stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT, check=False,
                )
                return {"exit_code": completed.returncode, "output": completed.stdout}

            core = mock.Mock()
            core.CONTROL = repo
            core.process.side_effect = process
            storage.recover_daemon_owned_control_changes(core)
            self.assertFalse(result.exists())

    def test_modified_task_file_remains_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
            subprocess.run(["git", "config", "user.name", "storage-test"], cwd=repo, check=True)
            subprocess.run(["git", "config", "user.email", "storage@example.invalid"], cwd=repo, check=True)
            task = repo / ".agent" / "tasks" / "pending.json"
            task.parent.mkdir(parents=True)
            task.write_text('{"id":"pending"}\n', encoding="utf-8")
            subprocess.run(["git", "add", ".agent/tasks/pending.json"], cwd=repo, check=True)
            subprocess.run(["git", "commit", "-qm", "base"], cwd=repo, check=True)
            task.write_text('{"id":"changed"}\n', encoding="utf-8")

            def process(args, cwd, **_kwargs):
                completed = subprocess.run(
                    args,
                    cwd=cwd,
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    check=False,
                )
                return {"exit_code": completed.returncode, "output": completed.stdout}

            core = mock.Mock()
            core.CONTROL = repo
            core.process.side_effect = process
            with self.assertRaisesRegex(RuntimeError, "unexpected local changes"):
                storage.recover_daemon_owned_control_changes(core)
            self.assertEqual(task.read_text(encoding="utf-8"), '{"id":"changed"}\n')

    def test_host_recovery_restores_only_own_status_and_result(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
            subprocess.run(["git", "config", "user.name", "storage-test"], cwd=repo, check=True)
            subprocess.run(["git", "config", "user.email", "storage@example.invalid"], cwd=repo, check=True)
            status = repo / ".agent/hosts/worker-a/status/daemon.json"
            status.parent.mkdir(parents=True)
            status.write_text('{"state":"idle"}\n', encoding="utf-8")
            subprocess.run(["git", "add", ".agent"], cwd=repo, check=True)
            subprocess.run(["git", "commit", "-qm", "base"], cwd=repo, check=True)
            status.write_text('{"state":"running"}\n', encoding="utf-8")
            result = repo / ".agent/hosts/worker-a/results/task.json"
            result.parent.mkdir(parents=True)
            result.write_text('{"status":"done"}\n', encoding="utf-8")

            def process(args, cwd, **_kwargs):
                completed = subprocess.run(
                    args, cwd=cwd, capture_output=True, text=True, check=False,
                )
                return {"exit_code": completed.returncode,
                        "output": completed.stdout + completed.stderr}

            core = SimpleNamespace(CONTROL=repo, process=process, log=mock.Mock())
            storage.recover_daemon_owned_control_changes(
                core, tasks_dir=".agent/hosts/worker-a/tasks",
            )
            self.assertEqual(status.read_text(encoding="utf-8"), '{"state":"idle"}\n')
            self.assertFalse(result.exists())
            git_status = subprocess.run(
                ["git", "status", "--porcelain"], cwd=repo,
                capture_output=True, text=True, check=True,
            )
            self.assertEqual(git_status.stdout, "")

    def test_host_recovery_refuses_other_host_legacy_and_host_task_changes(self) -> None:
        for relative in (
            ".agent/hosts/worker-b/results/result.json",
            ".agent/results/legacy.json",
            ".agent/hosts/worker-a/tasks/pending.json",
            ".agent/hosts/worker-a/identity.json",
            ".agent/hosts/worker-a/payloads/other.json",
        ):
            with self.subTest(relative=relative), tempfile.TemporaryDirectory() as tmp:
                repo = Path(tmp)
                subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
                item = repo / relative
                item.parent.mkdir(parents=True)
                item.write_text("protected\n", encoding="utf-8")

                def process(args, cwd, **_kwargs):
                    completed = subprocess.run(
                        args, cwd=cwd, capture_output=True, text=True, check=False,
                    )
                    return {"exit_code": completed.returncode,
                            "output": completed.stdout + completed.stderr}

                core = SimpleNamespace(CONTROL=repo, process=process, log=mock.Mock())
                with self.assertRaisesRegex(RuntimeError, "unexpected local changes"):
                    storage.recover_daemon_owned_control_changes(
                        core, tasks_dir=".agent/hosts/worker-a/tasks",
                    )
                self.assertEqual(item.read_text(encoding="utf-8"), "protected\n")

    def test_runtime_gc_drops_only_resource_lease_descriptors(self) -> None:
        core = SimpleNamespace(
            ENV={
                "PATH": "/usr/bin",
                LEASE_FDS_ENV: "7,8",
                RESOURCE_LEASE_FDS_ENV: "9,10",
            }
        )
        environment = cleanup._control_git_environment(core)
        self.assertIsNotNone(environment)
        assert environment is not None
        self.assertEqual(environment["PATH"], "/usr/bin")
        self.assertEqual(environment[LEASE_FDS_ENV], "7,8")
        self.assertNotIn(RESOURCE_LEASE_FDS_ENV, environment)


if __name__ == "__main__":
    unittest.main()
