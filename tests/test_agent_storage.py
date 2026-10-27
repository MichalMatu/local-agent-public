from __future__ import annotations

import subprocess
import tempfile
import unittest
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import local_agent.foundation.storage as storage


class StoragePolicyTests(unittest.TestCase):
    def test_bounded_pull_keeps_control_history_at_256(self) -> None:
        self.assertEqual(storage.CONTROL_HISTORY_DEPTH, 256)
        self.assertEqual(
            storage.bounded_control_pull_args("agent-control"),
            [
                "pull",
                "--rebase",
                "--depth",
                "256",
                "--no-tags",
                "origin",
                "+refs/heads/agent-control:refs/remotes/origin/agent-control",
            ],
        )

    def test_sync_control_skips_checkout_when_already_on_control_branch(self) -> None:
        process = mock.Mock(side_effect=[
            {"exit_code": 0, "output": ""},
            {"exit_code": 0, "output": "agent-control"},
            {"exit_code": 0, "output": "Already up to date."},
        ])
        core = SimpleNamespace(CONTROL=Path("/tmp/control"), CONTROL_BRANCH="agent-control", CONTROL_GIT_LOCK=nullcontext(), process=process)
        storage.sync_control(core)
        self.assertEqual(process.call_count, 3)
        self.assertEqual(process.call_args_list[0].args[0], ["git", "status", "--porcelain=v1", "-z", "--untracked-files=all"])
        self.assertEqual(process.call_args_list[1].args[0], ["git", "symbolic-ref", "--quiet", "--short", "HEAD"])
        self.assertEqual(process.call_args_list[2].args[0], ["git", *storage.bounded_control_pull_args("agent-control")])

    def test_sync_control_checks_out_when_control_branch_is_not_active(self) -> None:
        process = mock.Mock(side_effect=[
            {"exit_code": 0, "output": ""},
            {"exit_code": 0, "output": "main"},
            {"exit_code": 0, "output": ""},
            {"exit_code": 0, "output": "Already up to date."},
        ])
        core = SimpleNamespace(CONTROL=Path("/tmp/control"), CONTROL_BRANCH="agent-control", CONTROL_GIT_LOCK=nullcontext(), process=process)
        storage.sync_control(core)
        self.assertEqual(process.call_count, 4)
        self.assertEqual(process.call_args_list[2].args, (["git", "checkout", "agent-control"], Path("/tmp/control")))
        self.assertEqual(process.call_args_list[3].args[0], ["git", *storage.bounded_control_pull_args("agent-control")])

    def test_host_sync_does_not_run_legacy_gc_or_history_compaction(self) -> None:
        process = mock.Mock(side_effect=[
            {"exit_code": 0, "output": ""},
            {"exit_code": 0, "output": "agent-control"},
            {"exit_code": 0, "output": "Already up to date."},
        ])
        core = SimpleNamespace(
            CONTROL=Path("/tmp/control"), CONTROL_BRANCH="agent-control",
            CONTROL_GIT_LOCK=nullcontext(), process=process,
        )
        with mock.patch("local_agent.repository.cleanup.prune_control_runtime") as gc:
            storage.sync_control(core, tasks_dir=".agent/hosts/worker-a/tasks")
        gc.assert_not_called()
        self.assertEqual(process.call_count, 3)

    def test_selected_host_mailbox_must_be_canonical_before_recovery(self) -> None:
        for path in (
            ".agent/hosts/../tasks", ".agent/hosts/Worker-A/tasks",
            ".agent/hosts/worker-a/results", ".agent/hosts/worker-a/tasks/extra",
        ):
            with self.subTest(path=path):
                core = SimpleNamespace(process=mock.Mock())
                with self.assertRaisesRegex(ValueError, "invalid selected control task mailbox"):
                    storage.recover_daemon_owned_control_changes(core, tasks_dir=path)
                core.process.assert_not_called()

    def test_sync_control_recovers_only_daemon_owned_dirty_paths(self) -> None:
        process = mock.Mock(side_effect=[
            {"exit_code": 0, "output": " M .agent/status/daemon.json\0?? .agent/runs/task.json\0"},
            {"exit_code": 0, "output": ""},
            {"exit_code": 0, "output": ""},
            {"exit_code": 0, "output": ""},
            {"exit_code": 0, "output": "agent-control"},
            {"exit_code": 0, "output": "Already up to date."},
        ])
        core = SimpleNamespace(
            CONTROL=Path("/tmp/control"),
            CONTROL_BRANCH="agent-control",
            CONTROL_GIT_LOCK=nullcontext(),
            process=process,
            log=mock.Mock(),
        )
        storage.sync_control(core)
        self.assertEqual(
            process.call_args_list[1].args[0],
            [
                "git",
                "restore",
                "--source=HEAD",
                "--staged",
                "--worktree",
                "--",
                ".agent/status/daemon.json",
            ],
        )
        self.assertEqual(
            process.call_args_list[2].args[0],
            ["git", "clean", "-fd", "--", ".agent/runs/task.json"],
        )
        self.assertIn("safe control checkout changes", core.log.call_args.args[0])

    def test_sync_control_recovers_untracked_only_without_restore(self) -> None:
        process = mock.Mock(side_effect=[
            {"exit_code": 0, "output": "?? .agent/results/new result.json\0"},
            {"exit_code": 0, "output": ""},
            {"exit_code": 0, "output": ""},
            {"exit_code": 0, "output": "agent-control"},
            {"exit_code": 0, "output": "Already up to date."},
        ])
        core = SimpleNamespace(
            CONTROL=Path("/tmp/control"),
            CONTROL_BRANCH="agent-control",
            CONTROL_GIT_LOCK=nullcontext(),
            process=process,
            log=mock.Mock(),
        )
        storage.sync_control(core)
        self.assertEqual(
            process.call_args_list[1].args[0],
            ["git", "clean", "-fd", "--", ".agent/results/new result.json"],
        )
        self.assertFalse(
            any(call.args[0][1] == "restore" for call in process.call_args_list)
        )

    def test_sync_control_recovers_untracked_ds_store_noise(self) -> None:
        process = mock.Mock(side_effect=[
            {"exit_code": 0, "output": "?? .DS_Store\0"},
            {"exit_code": 0, "output": ""},
            {"exit_code": 0, "output": ""},
            {"exit_code": 0, "output": "agent-control"},
            {"exit_code": 0, "output": "Already up to date."},
        ])
        core = SimpleNamespace(
            CONTROL=Path("/tmp/control"),
            CONTROL_BRANCH="agent-control",
            CONTROL_GIT_LOCK=nullcontext(),
            process=process,
            log=mock.Mock(),
        )
        storage.sync_control(core)
        self.assertEqual(
            process.call_args_list[1].args[0],
            ["git", "clean", "-fd", "--", ".DS_Store"],
        )
        core.log.assert_not_called()

    def test_sync_control_rejects_other_untracked_host_metadata(self) -> None:
        process = mock.Mock(return_value={
            "exit_code": 0,
            "output": "?? .localized\0",
        })
        core = SimpleNamespace(
            CONTROL=Path("/tmp/control"),
            CONTROL_BRANCH="agent-control",
            CONTROL_GIT_LOCK=nullcontext(),
            process=process,
            log=mock.Mock(),
        )
        with self.assertRaisesRegex(RuntimeError, "unexpected local changes"):
            storage.sync_control(core)
        process.assert_called_once()

    def test_sync_control_rejects_unexpected_dirty_paths(self) -> None:
        process = mock.Mock(return_value={
            "exit_code": 0,
            "output": " M .agent/tasks/task.json\0",
        })
        core = SimpleNamespace(
            CONTROL=Path("/tmp/control"),
            CONTROL_BRANCH="agent-control",
            CONTROL_GIT_LOCK=nullcontext(),
            process=process,
            log=mock.Mock(),
        )
        with self.assertRaisesRegex(RuntimeError, "unexpected local changes"):
            storage.sync_control(core)
        process.assert_called_once()

    def test_control_recovery_handles_real_mixed_checkout(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
            subprocess.run(["git", "config", "user.name", "storage-test"], cwd=repo, check=True)
            subprocess.run(["git", "config", "user.email", "storage@example.invalid"], cwd=repo, check=True)
            tracked = repo / ".agent" / "status" / "daemon.json"
            tracked.parent.mkdir(parents=True)
            tracked.write_text("old\n", encoding="utf-8")
            subprocess.run(["git", "add", ".agent/status/daemon.json"], cwd=repo, check=True)
            subprocess.run(["git", "commit", "-qm", "base"], cwd=repo, check=True)
            tracked.write_text("new\n", encoding="utf-8")
            untracked = repo / ".agent" / "results" / "new result.json"
            untracked.parent.mkdir(parents=True)
            untracked.write_text("{}\n", encoding="utf-8")

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

            core = SimpleNamespace(CONTROL=repo, process=process, log=mock.Mock())
            storage.recover_daemon_owned_control_changes(core)
            self.assertEqual(tracked.read_text(encoding="utf-8"), "old\n")
            self.assertFalse(untracked.exists())
            status = subprocess.run(
                ["git", "status", "--porcelain=v1"],
                cwd=repo,
                text=True,
                stdout=subprocess.PIPE,
                check=True,
            )
            self.assertEqual(status.stdout, "")

    def test_control_recovery_removes_real_ds_store_noise(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
            noise = repo / ".DS_Store"
            noise.write_text("finder metadata\n", encoding="utf-8")

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

            core = SimpleNamespace(CONTROL=repo, process=process, log=mock.Mock())
            storage.recover_daemon_owned_control_changes(core)
            self.assertFalse(noise.exists())
            core.log.assert_not_called()
            exclude = repo / ".git" / "info" / "exclude"
            original = exclude.read_text(encoding="utf-8")
            self.assertIn(".DS_Store", original.splitlines())

            # Finder recreates the noise even after a successful cleanup.
            # Checkout-local exclude suppresses the next poll and all paths.
            noise.write_text("second Finder visit", encoding="utf-8")
            nested = repo / ".agent" / "status" / ".DS_Store"
            nested.parent.mkdir(parents=True, exist_ok=True)
            nested.write_text("nested Finder visit", encoding="utf-8")
            status = subprocess.run(
                ["git", "status", "--porcelain=v1", "--untracked-files=all"],
                cwd=repo, text=True, stdout=subprocess.PIPE, check=True,
            )
            self.assertEqual(status.stdout, "")
            storage.recover_daemon_owned_control_changes(core)
            core.log.assert_not_called()
            self.assertEqual(exclude.read_text(encoding="utf-8"), original)
            self.assertTrue(noise.exists())  # ignored, not endlessly re-cleaned
            self.assertTrue(nested.exists())

    def test_recovery_preserves_checkout_local_excludes_and_other_unknown_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
            exclude = repo / ".git" / "info" / "exclude"
            original = "# local user patterns\n*.private-log\n"
            exclude.write_text(original, encoding="utf-8")
            (repo / ".DS_Store").write_text("finder metadata", encoding="utf-8")

            def process(args, cwd, **_kwargs):
                run = subprocess.run(
                    args, cwd=cwd, text=True, stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT, check=False,
                )
                return {"exit_code": run.returncode, "output": run.stdout}

            core = SimpleNamespace(CONTROL=repo, process=process, log=mock.Mock())
            storage.recover_daemon_owned_control_changes(core)
            self.assertEqual(
                exclude.read_text(encoding="utf-8"),
                original + ".DS_Store\n",
            )
            unknown = repo / ".localized"
            unknown.write_text("not approved for cleanup", encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "unexpected local changes"):
                storage.recover_daemon_owned_control_changes(core)
            self.assertTrue(unknown.exists())
            self.assertEqual(
                exclude.read_text(encoding="utf-8"),
                original + ".DS_Store\n",
            )

    def test_recovery_does_not_follow_checkout_exclude_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "checkout"
            repo.mkdir()
            subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
            outside = root / "unrelated-info"
            outside.write_text("preserve\n", encoding="utf-8")
            exclude = repo / ".git" / "info" / "exclude"
            exclude.unlink()
            exclude.symlink_to(outside)
            (repo / ".DS_Store").write_text("finder", encoding="utf-8")

            def process(args, cwd, **_kwargs):
                run = subprocess.run(
                    args, cwd=cwd, text=True, stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT, check=False,
                )
                return {"exit_code": run.returncode, "output": run.stdout}

            core = SimpleNamespace(CONTROL=repo, process=process, log=mock.Mock())
            storage.recover_daemon_owned_control_changes(core)
            self.assertEqual(outside.read_text(encoding="utf-8"), "preserve\n")
            self.assertFalse((repo / ".DS_Store").exists())
            core.log.assert_not_called()

    def test_control_recovery_rejects_modified_tracked_ds_store(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
            subprocess.run(["git", "config", "user.name", "test"], cwd=repo, check=True)
            subprocess.run(["git", "config", "user.email", "test@example.invalid"],
                           cwd=repo, check=True)
            noise = repo / ".DS_Store"
            noise.write_text("tracked", encoding="utf-8")
            subprocess.run(["git", "add", "-f", ".DS_Store"], cwd=repo, check=True)
            subprocess.run(["git", "commit", "-qm", "track Finder metadata"],
                           cwd=repo, check=True)
            noise.write_text("new tracked state", encoding="utf-8")

            def process(args, cwd, **_kwargs):
                run = subprocess.run(
                    args, cwd=cwd, text=True, stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT, check=False,
                )
                return {"exit_code": run.returncode, "output": run.stdout}

            core = SimpleNamespace(CONTROL=repo, process=process, log=mock.Mock())
            with self.assertRaisesRegex(RuntimeError, "unexpected local changes"):
                storage.recover_daemon_owned_control_changes(core)
            self.assertEqual(noise.read_text(encoding="utf-8"), "new tracked state")

    def test_control_recovery_refuses_real_task_change(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
            task = repo / ".agent" / "tasks" / "task.json"
            task.parent.mkdir(parents=True)
            task.write_text("{}\n", encoding="utf-8")

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

            core = SimpleNamespace(CONTROL=repo, process=process, log=mock.Mock())
            with self.assertRaisesRegex(RuntimeError, "unexpected local changes"):
                storage.recover_daemon_owned_control_changes(core)
            self.assertTrue(task.exists())

    def test_transient_ssh_failure_is_retried_and_recovers(self) -> None:
        process = mock.Mock(
            side_effect=[
                {
                    "exit_code": 1,
                    "output": (
                        "Connection closed by 140.82.121.36 port 443\n"
                        "fatal: Could not read from remote repository."
                    ),
                },
                {"exit_code": 0, "output": "Already up to date."},
            ]
        )
        core = SimpleNamespace(process=process, log=mock.Mock())

        with mock.patch.object(storage.time, "sleep") as sleep:
            result = storage.run_git_with_network_retry(
                core,
                ["git", "pull", "origin", "agent-control"],
                Path("/tmp/control"),
            )

        self.assertEqual(result["exit_code"], 0)
        self.assertEqual(process.call_count, 2)
        sleep.assert_called_once_with(2.0)
        self.assertIn("retrying in 2s", core.log.call_args.args[0])

    def test_transient_https_gateway_failure_is_retried(self) -> None:
        process = mock.Mock(side_effect=[
            {"exit_code": 1, "output": "fatal: unable to access URL: The requested URL returned error: 503"},
            {"exit_code": 0, "output": ""},
        ])
        core = SimpleNamespace(process=process, log=mock.Mock())
        with mock.patch.object(storage.time, "sleep") as sleep:
            result = storage.run_git_with_network_retry(core, ["git", "fetch", "origin"], Path("/tmp/control"))
        self.assertEqual(result["exit_code"], 0)
        sleep.assert_called_once_with(2.0)

    def test_deterministic_git_failure_is_not_retried(self) -> None:
        process = mock.Mock(
            return_value={
                "exit_code": 1,
                "output": "git@github.com: Permission denied (publickey).",
            }
        )
        core = SimpleNamespace(process=process, log=mock.Mock())

        with mock.patch.object(storage.time, "sleep") as sleep:
            result = storage.run_git_with_network_retry(
                core,
                ["git", "pull", "origin", "agent-control"],
                Path("/tmp/control"),
            )

        self.assertEqual(result["exit_code"], 1)
        process.assert_called_once()
        sleep.assert_not_called()
        core.log.assert_not_called()

    def test_timeout_is_treated_as_transient_transport_failure(self) -> None:
        process = mock.Mock(
            side_effect=[
                {"exit_code": 124, "output": "", "timed_out": True},
                {"exit_code": 0, "output": ""},
            ]
        )
        core = SimpleNamespace(process=process, log=mock.Mock())

        with mock.patch.object(storage.time, "sleep") as sleep:
            result = storage.run_git_with_network_retry(
                core,
                ["git", "fetch", "origin", "main"],
                Path("/tmp/repo"),
            )

        self.assertEqual(result["exit_code"], 0)
        sleep.assert_called_once_with(2.0)

    def test_silent_terminal_failure_gets_nonempty_diagnostic(self) -> None:
        process = mock.Mock(
            return_value={
                "exit_code": 126,
                "output": "",
                "background_process_leak": True,
                "failure_reason": "background_process_leak",
                "elapsed_seconds": 0.25,
            }
        )
        core = SimpleNamespace(process=process, log=mock.Mock())

        result = storage.run_git_with_network_retry(
            core,
            ["git", "fetch", "origin", "main"],
            Path("/tmp/repo"),
            retry_delays=(),
        )

        self.assertEqual(result["exit_code"], 126)
        self.assertIn("exit_code=126", result["output"])
        self.assertIn("background_process_leak=true", result["output"])
        self.assertIn("failure_reason=background_process_leak", result["output"])

    def test_silent_timeout_after_retries_gets_nonempty_diagnostic(self) -> None:
        process = mock.Mock(
            return_value={
                "exit_code": 124,
                "output": "",
                "timed_out": True,
                "elapsed_seconds": 60.0,
            }
        )
        core = SimpleNamespace(process=process, log=mock.Mock())

        with mock.patch.object(storage.time, "sleep"):
            result = storage.run_git_with_network_retry(
                core,
                ["git", "fetch", "origin", "main"],
                Path("/tmp/repo"),
                retry_delays=(0.0,),
            )

        self.assertEqual(result["exit_code"], 124)
        self.assertIn("timed_out=true", result["output"])
        self.assertNotEqual(result["output"].strip(), "")


if __name__ == "__main__":
    unittest.main()
