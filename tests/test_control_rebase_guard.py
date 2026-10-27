"""Fail-closed control Git interrupted-rebase admission tests.

No tests ever remove, abort, reset or overwrite a live control checkout.
"""
from __future__ import annotations

import subprocess
import tempfile
import unittest
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from local_agent.foundation import storage
from local_agent.repository import cleanup, compaction


class InterruptedControlRebaseGuardTests(unittest.TestCase):
    def test_gc_recovery_only_accepts_flat_bounded_json_task_deletions(self):
        helper = storage._recoverable_runtime_task_deletion
        self.assertTrue(helper("D", ".agent/tasks/finished.json"))
        self.assertTrue(helper(" D", ".agent/tasks/finished.json"))
        for code, path in (
            ("MD", ".agent/tasks/finished.json"),
            ("AD", ".agent/tasks/finished.json"),
            ("DD", ".agent/tasks/finished.json"),
            ("M", ".agent/tasks/finished.json"),
            ("A", ".agent/tasks/finished.json"),
            ("D", ".agent/tasks/nested/finished.json"),
            ("D", ".agent/tasks/readme.md"),
            ("D", ".agent/tasks/"),
            ("D", ".agent/hosts/other/tasks/finished.json"),
            ("D", ".agent/tasks/" + "x" * 201 + ".json"),
        ):
            with self.subTest(code=code, path=path):
                self.assertFalse(helper(code, path))

    def test_managed_interrupted_publication_rebuilds_without_erasing_execution_ledger(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            remote, local, peer = root / "remote.git", root / "local", root / "peer"

            def git(directory, *args, check=True):
                return subprocess.run(["git", *args], cwd=directory, check=check,
                                      capture_output=True, text=True, timeout=12)

            git(root, "init", "--bare", "--initial-branch=agent-control", str(remote))
            git(root, "clone", str(remote), str(local))
            for checkout in [local]:
                git(checkout, "config", "user.name", "Test")
                git(checkout, "config", "user.email", "test@example.invalid")
            status = local / ".agent/status/daemon.json"
            status.parent.mkdir(parents=True)
            status.write_text('{"state":"base"}\n')
            finished_task = local / ".agent/tasks/finished.json"
            finished_task.parent.mkdir(parents=True)
            finished_task.write_text('{"id":"finished"}\n')
            old_result = local / ".agent/results/finished.json"
            old_result.parent.mkdir(parents=True)
            old_result.write_text('{"id":"finished","status":"done"}\n')
            git(local, "add", ".agent")
            git(local, "commit", "-m", "Base")
            git(local, "push", "origin", "agent-control")
            git(root, "clone", str(remote), str(peer))
            git(peer, "config", "user.name", "Peer")
            git(peer, "config", "user.email", "peer@example.invalid")
            status.write_text('{"state":"local"}\n')
            finished_task.unlink()
            old_result.unlink()
            git(local, "add", "-A", ".agent")
            git(local, "commit", "-m", "Unpublished status and completed task GC")
            local_tip = git(local, "rev-parse", "HEAD").stdout.strip()
            old_recovery = "refs/local-agent/recovery/interrupted-rebase"
            old_tip = git(local, "rev-parse", "HEAD^").stdout.strip()
            git(local, "update-ref", old_recovery, old_tip)
            (peer / ".agent/status/daemon.json").write_text('{"state":"remote"}\n')
            tasks = peer / ".agent/tasks"
            tasks.mkdir(exist_ok=True)
            (tasks / "new.json").write_text('{"id":"new"}\n')
            git(peer, "add", ".agent/status/daemon.json", ".agent/tasks/new.json")
            git(peer, "commit", "-m", "New remote task and status")
            git(peer, "push", "origin", "agent-control")
            self.assertNotEqual(git(local, "pull", "--rebase", check=False).returncode, 0)
            from local_agent.supervisor.host_control import HostControlSource, validate_host_control_checkout

            source = HostControlSource("worker-test", "11111111-2222-4333-8444-555555555555", local)
            git(local, "remote", "set-url", "origin", "https://github.com/example-owner/local-agent.git")
            validate_host_control_checkout(source, production_checkout=root / "installed")
            metadata = local / ".git/rebase-merge/head-name"
            original = metadata.read_text()
            metadata.write_text("refs/heads/other\n")
            with self.assertRaisesRegex(ValueError, "expected control branch"):
                validate_host_control_checkout(source, production_checkout=root / "installed")
            metadata.write_text(original)
            git(local, "remote", "set-url", "origin", str(remote))
            ledger = root / "durable-result-spool.json"
            ledger.write_text('{"id":"already-executed","status":"failed"}\n')
            before = ledger.read_bytes()

            def process(args, cwd, **_kwargs):
                result = subprocess.run(args, cwd=cwd, capture_output=True, text=True, timeout=12)
                return {"exit_code": result.returncode, "output": result.stdout + result.stderr}

            core = SimpleNamespace(CONTROL=local, CONTROL_BRANCH="agent-control",
                                   CONTROL_GIT_LOCK=nullcontext(), process=process)
            with mock.patch("local_agent.repository.cleanup.prune_control_runtime"):
                storage.sync_control(core)
            self.assertEqual(ledger.read_bytes(), before)
            self.assertTrue((local / ".agent/tasks/new.json").is_file())
            self.assertEqual(git(local, "rev-parse", old_recovery).stdout.strip(), old_tip)
            self.assertEqual(git(local, "rev-parse",
                                 f"refs/local-agent/recovery/interrupted-rebase-{local_tip}")
                             .stdout.strip(), local_tip)
            storage.require_control_rebase_clear(local)

    def test_rebase_recovery_refuses_modified_owner_task_even_in_legacy_mailbox(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            metadata = {
                "head-name": "refs/heads/agent-control",
                "orig-head": "a" * 40,
                "onto": "b" * 40,
            }
            commands = []

            def process(args, _cwd, **_kwargs):
                commands.append(args)
                if args[1] == "merge-base":
                    return {"exit_code": 0, "output": "c" * 40}
                if args[1] == "diff":
                    return {
                        "exit_code": 0,
                        "output": "M\0.agent/tasks/owner-task.json\0",
                    }
                raise AssertionError("recovery attempted a Git effect")

            core = SimpleNamespace(
                CONTROL=root, CONTROL_BRANCH="agent-control", process=process,
            )
            with (
                mock.patch.object(storage, "interrupted_control_rebase_metadata",
                                  return_value=metadata),
                mock.patch.object(storage, "recover_daemon_owned_control_changes"),
            ):
                with self.assertRaisesRegex(RuntimeError, "non-publication commits"):
                    storage.recover_interrupted_control_rebase(core)
            self.assertEqual([c[1] for c in commands], ["merge-base", "diff"])

    def test_both_git_rebase_state_directories_block_sync_without_git_commands(self):
        for dirname in ("rebase-merge", "rebase-apply"):
            with self.subTest(dirname=dirname), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                state = root / ".git" / dirname
                state.mkdir(parents=True)
                (state / "orig-head").write_text("a" * 40 + "\n", encoding="utf-8")
                core = SimpleNamespace(
                    CONTROL=root, CONTROL_BRANCH="agent-control",
                    CONTROL_GIT_LOCK=nullcontext(), process=mock.Mock(),
                )
                with self.assertRaisesRegex(RuntimeError, f"unfinished Git {dirname}"):
                    storage.sync_control(core)
                core.process.assert_not_called()
                self.assertEqual((state / "orig-head").read_text(), "a" * 40 + "\n")

    def test_refuse_to_publish_without_rebase_abort_or_push(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".git" / "rebase-merge").mkdir(parents=True)
            core = SimpleNamespace(CONTROL=root, CONTROL_BRANCH="agent-control")
            with mock.patch.object(storage, "run_git_with_network_retry") as run:
                with self.assertRaisesRegex(RuntimeError, "refusing control pull/publish"):
                    storage.publish_control_commit(core)
            run.assert_not_called()

    def test_worktree_gitdir_pointer_is_checked(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            checkout = root / "checkout"
            checkout.mkdir()
            actual = root / "common-git" / "worktrees" / "checkout"
            (actual / "rebase-merge").mkdir(parents=True)
            (checkout / ".git").write_text(
                f"gitdir: {actual}\n", encoding="utf-8",
            )
            with self.assertRaisesRegex(RuntimeError, "unfinished Git rebase-merge"):
                storage.require_control_rebase_clear(checkout)

    def test_worktree_gitdir_pointer_must_resolve_to_an_existing_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            checkout = root / "checkout"
            checkout.mkdir()
            dotgit = checkout / ".git"
            text = "gitdir: ../missing-gitdir\n"
            dotgit.write_text(text, encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "gitdir target missing"):
                storage.require_control_rebase_clear(checkout)
            self.assertEqual(dotgit.read_text(encoding="utf-8"), text)
            # Valid non-rebasing linked metadata can pass the same read-only
            # preflight, without a Git process or any permission changes.
            (root / "missing-gitdir").mkdir()
            storage.require_control_rebase_clear(checkout)

    def test_worktree_gitdir_symlink_target_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            checkout = root / "checkout"
            checkout.mkdir()
            actual = root / "actual-gitdir"
            actual.mkdir()
            (root / "metadata-link").symlink_to(
                actual, target_is_directory=True,
            )
            (checkout / ".git").write_text(
                "gitdir: ../metadata-link\n", encoding="utf-8",
            )
            with self.assertRaisesRegex(RuntimeError, "gitdir target missing or symlinked"):
                storage.require_control_rebase_clear(checkout)

    def test_real_conflicting_git_rebase_is_detected_without_losing_unpublished_commit(self):
        def git(directory: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
            return subprocess.run(
                ["git", *args], cwd=directory, check=check,
                capture_output=True, text=True, timeout=12,
            )

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            remote = root / "origin.git"
            remote.mkdir()
            git(root, "init", "--bare", "--initial-branch=agent-control", str(remote))
            local = root / "local"
            peer = root / "peer"
            git(root, "clone", str(remote), str(local))
            git(local, "config", "user.name", "Audit")
            git(local, "config", "user.email", "audit@example.invalid")
            (local / "shared.txt").write_text("base\n", encoding="utf-8")
            git(local, "add", "shared.txt")
            git(local, "commit", "-m", "base")
            git(local, "push", "origin", "agent-control")
            git(root, "clone", str(remote), str(peer))
            git(peer, "config", "user.name", "Audit peer")
            git(peer, "config", "user.email", "peer@example.invalid")
            (local / "shared.txt").write_text("unpublished local\n", encoding="utf-8")
            git(local, "commit", "-am", "important unpublished status")
            unpublished = git(local, "rev-parse", "agent-control").stdout.strip()
            (peer / "shared.txt").write_text("remote divergent\n", encoding="utf-8")
            git(peer, "commit", "-am", "concurrent status")
            git(peer, "push", "origin", "agent-control")
            attempted = git(
                local, "pull", "--rebase", "origin", "agent-control", check=False,
            )
            self.assertNotEqual(attempted.returncode, 0)
            self.assertTrue((local / ".git" / "rebase-merge").is_dir())
            with self.assertRaisesRegex(RuntimeError, "unfinished Git rebase-merge"):
                storage.require_control_rebase_clear(local)
            self.assertEqual(
                git(local, "rev-parse", "refs/heads/agent-control").stdout.strip(),
                unpublished,
            )

    def test_legacy_runtime_gc_does_not_touch_stale_rebase_checkout(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            rebase = root / ".git" / "rebase-merge"
            rebase.mkdir(parents=True)
            marker = rebase / "orig-head"
            marker.write_text("a" * 40 + "\n", encoding="utf-8")
            core = SimpleNamespace(
                CONTROL=root, CONTROL_BRANCH="agent-control",
                CONTROL_GIT_LOCK=nullcontext(), process=mock.Mock(),
            )
            with mock.patch.object(cleanup, "control_cleanup_plan") as plan:
                with self.assertRaisesRegex(RuntimeError, "unfinished Git rebase-merge"):
                    cleanup.prune_control_runtime(core)
            plan.assert_not_called()
            core.process.assert_not_called()
            self.assertEqual(marker.read_text(encoding="utf-8"), "a" * 40 + "\n")

    def test_direct_runtime_history_compaction_is_guarded(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            rebase = root / ".git" / "rebase-apply"
            rebase.mkdir(parents=True)
            core = SimpleNamespace(CONTROL=root, process=mock.Mock())
            with self.assertRaisesRegex(RuntimeError, "unfinished Git rebase-apply"):
                cleanup._compact_control_history_locked(core)
            core.process.assert_not_called()
            self.assertTrue(rebase.is_dir())

    def test_runtime_gc_rechecks_before_pull_if_rebase_appears_after_local_commit(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            gitdir = root / ".git"
            gitdir.mkdir()
            result = root / ".agent" / "runs" / "old.json"
            result.parent.mkdir(parents=True)
            result.write_text("{}\n", encoding="utf-8")

            def fake_git(args, _cwd, **_kwargs):
                if args[1] == "commit":
                    (gitdir / "rebase-merge").mkdir()
                    return {"exit_code": 0, "output": ""}
                if args[1] == "diff":
                    return {"exit_code": 1, "output": ""}
                if args[1] == "add":
                    return {"exit_code": 0, "output": ""}
                raise AssertionError(f"unexpected Git command after rebase: {args}")

            core = SimpleNamespace(
                CONTROL=root, CONTROL_BRANCH="agent-control",
                CONTROL_GIT_LOCK=nullcontext(), process=mock.Mock(side_effect=fake_git),
            )
            with mock.patch.object(cleanup, "control_cleanup_plan", return_value=(".agent/runs/old.json",)):
                with mock.patch.object(storage, "run_git_with_network_retry") as network:
                    with self.assertRaisesRegex(RuntimeError, "unfinished Git rebase-merge"):
                        cleanup.prune_control_runtime(core)
            network.assert_not_called()
            self.assertTrue((gitdir / "rebase-merge").is_dir())

    def test_administrative_control_compaction_refuses_stale_rebase(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            marker = root / ".git" / "rebase-merge"
            marker.mkdir(parents=True)
            with mock.patch.object(compaction.admin, "run_git") as run:
                with self.assertRaisesRegex(RuntimeError, "unfinished Git rebase-merge"):
                    compaction.plan_control_compaction(root, "agent-control")
                with self.assertRaisesRegex(RuntimeError, "unfinished Git rebase-merge"):
                    compaction._realign_control_checkout(root, "agent-control", "a" * 40)
            run.assert_not_called()
            self.assertTrue(marker.is_dir())

    def test_unexpected_git_files_fail_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".git").write_text("nonsense\n", encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "unexpected control checkout"):
                storage.require_control_rebase_clear(root)

    def test_normal_existing_git_checkout_is_allowed_without_writes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            subprocess.run(["git", "init", "-q", str(root)], check=True,
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=10)
            storage.require_control_rebase_clear(root)
            status = subprocess.run(["git", "-C", str(root), "status", "--porcelain"],
                                    check=True, capture_output=True, text=True, timeout=10)
            self.assertEqual(status.stdout, "")
            self.assertFalse((root / ".git" / "rebase-merge").exists())

    def test_symlinked_git_dir_is_not_followed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            gitdir = root / "external"
            gitdir.mkdir()
            (root / ".git").symlink_to(gitdir, target_is_directory=True)
            with self.assertRaisesRegex(RuntimeError, "symlink"):
                storage.require_control_rebase_clear(root)


if __name__ == "__main__":
    unittest.main()
