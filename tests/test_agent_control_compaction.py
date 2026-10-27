from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from local_agent.foundation import storage
from local_agent.repository import admin, compaction
from local_agent.repository.context import RepositoryContext


def git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    return result.stdout.strip()


def configure_identity(path: Path) -> None:
    git(path, "config", "user.name", "Local Agent Test")
    git(path, "config", "user.email", "local-agent-test@example.invalid")


class ControlRepositoryFixture:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.remote = root / "remote.git"
        self.seed = root / "seed"
        self.control = root / "control"

        git(root, "init", "--bare", str(self.remote))
        git(root, "clone", str(self.remote), str(self.seed))
        configure_identity(self.seed)
        git(self.seed, "switch", "--orphan", "agent-control")
        status_dir = self.seed / ".agent" / "status"
        status_dir.mkdir(parents=True)
        (status_dir / "daemon.json").write_text('{"generation":0}\n', encoding="utf-8")
        git(self.seed, "add", ".agent")
        git(self.seed, "commit", "-m", "Initialize control branch")
        for generation in range(1, 5):
            (status_dir / "daemon.json").write_text(
                f'{{"generation":{generation}}}\n', encoding="utf-8"
            )
            git(self.seed, "add", ".agent/status/daemon.json")
            git(self.seed, "commit", "-m", f"Control update {generation}")
        git(self.seed, "push", "-u", "origin", "agent-control")

        git(root, "clone", "--branch", "agent-control", str(self.remote), str(self.control))
        configure_identity(self.control)

    def remote_head(self) -> str:
        return git(self.remote, "rev-parse", "refs/heads/agent-control")

    def remote_commit_count(self) -> int:
        return int(git(self.remote, "rev-list", "--count", "refs/heads/agent-control"))

    def advance_remote(self) -> str:
        path = self.seed / ".agent" / "status" / "external.json"
        path.write_text('{"source":"external"}\n', encoding="utf-8")
        git(self.seed, "add", ".agent/status/external.json")
        git(self.seed, "commit", "-m", "Concurrent control update")
        git(self.seed, "push", "origin", "agent-control")
        return self.remote_head()

    def advance_rewritten_remote(self) -> str:
        writer = self.root / "post-compact-writer"
        git(self.root, "clone", "--branch", "agent-control", str(self.remote), str(writer))
        configure_identity(writer)
        path = writer / ".agent" / "status" / "post-compact.json"
        path.write_text('{"source":"post-compact"}\n', encoding="utf-8")
        git(writer, "add", ".agent/status/post-compact.json")
        git(writer, "commit", "-m", "Post-compaction remote update")
        git(writer, "push", "origin", "agent-control")
        return self.remote_head()


class AgentControlCompactionTests(unittest.TestCase):
    def test_dry_run_plan_does_not_mutate_local_or_remote_history(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fixture = ControlRepositoryFixture(Path(tmp))
            local_before = git(fixture.control, "rev-parse", "HEAD")
            remote_before = fixture.remote_head()
            count_before = fixture.remote_commit_count()

            plan = compaction.plan_control_compaction(
                fixture.control,
                "agent-control",
                threshold=3,
            )

            self.assertTrue(plan.eligible)
            self.assertEqual(plan.reason, "ready")
            self.assertEqual(plan.head_sha, local_before)
            self.assertEqual(plan.remote_sha, remote_before)
            self.assertEqual(git(fixture.control, "rev-parse", "HEAD"), local_before)
            self.assertEqual(fixture.remote_head(), remote_before)
            self.assertEqual(fixture.remote_commit_count(), count_before)

    def test_compaction_preserves_exact_tree_and_replaces_history_with_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fixture = ControlRepositoryFixture(Path(tmp))
            old_head = git(fixture.control, "rev-parse", "HEAD")
            old_tree = git(fixture.control, "rev-parse", "HEAD^{tree}")

            result = compaction.compact_control_history(
                fixture.control,
                "agent-control",
                threshold=3,
                expected_head=old_head,
            )

            self.assertTrue(result["changed"])
            new_head = git(fixture.control, "rev-parse", "HEAD")
            self.assertNotEqual(new_head, old_head)
            self.assertEqual(git(fixture.control, "rev-parse", "HEAD^{tree}"), old_tree)
            self.assertEqual(fixture.remote_head(), new_head)
            self.assertEqual(
                git(fixture.remote, "rev-parse", "refs/heads/agent-control^{tree}"),
                old_tree,
            )
            self.assertEqual(fixture.remote_commit_count(), 1)
            self.assertEqual(int(git(fixture.control, "rev-list", "--count", "HEAD")), 1)

    def test_shallow_checkout_migration_backup_rewrite_and_followup_pull(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fixture = ControlRepositoryFixture(root)
            shallow = root / "shallow-control"
            git(
                root,
                "clone",
                "--depth",
                "3",
                "--single-branch",
                "--branch",
                "agent-control",
                f"file://{fixture.remote}",
                str(shallow),
            )
            configure_identity(shallow)
            self.assertEqual(git(shallow, "rev-parse", "--is-shallow-repository"), "true")
            self.assertEqual(int(git(shallow, "rev-list", "--count", "HEAD")), 3)

            old_head = fixture.remote_head()
            old_tree = git(shallow, "rev-parse", "HEAD^{tree}")
            repository = RepositoryContext(
                repository_id="project-shallow",
                repository="owner/project-shallow",
                control=shallow,
                work=root / "work",
                checkpoints=root / "checkpoints",
            )
            backup_dir = root / "backups"

            with mock.patch.object(admin, "clone_url", return_value=str(fixture.remote)):
                bundle = compaction.backup_control_history(
                    repository,
                    backup_dir,
                    expected_head=old_head,
                )

            self.assertTrue(bundle.is_file())
            self.assertIn(old_head, git(root, "bundle", "list-heads", str(bundle)))

            result = compaction.compact_control_history(
                shallow,
                "agent-control",
                threshold=3,
                expected_head=old_head,
            )
            self.assertTrue(result["changed"])
            self.assertEqual(fixture.remote_commit_count(), 1)
            self.assertEqual(git(shallow, "rev-parse", "HEAD^{tree}"), old_tree)

            advanced_head = fixture.advance_rewritten_remote()
            git(shallow, *storage.bounded_control_pull_args("agent-control"))
            self.assertEqual(git(shallow, "rev-parse", "HEAD"), advanced_head)
            self.assertTrue((shallow / ".agent/status/post-compact.json").is_file())

    def test_force_with_lease_rejects_concurrent_remote_update_without_data_loss(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fixture = ControlRepositoryFixture(Path(tmp))
            old_head = git(fixture.control, "rev-parse", "HEAD")
            original_run_git = admin.run_git
            concurrent_head: list[str] = []
            injected = False

            def racing_run_git(args, *, cwd=None, timeout=300):
                nonlocal injected
                if args and args[0] == "push" and not injected:
                    injected = True
                    concurrent_head.append(fixture.advance_remote())
                return original_run_git(args, cwd=cwd, timeout=timeout)

            with mock.patch.object(admin, "run_git", side_effect=racing_run_git):
                with self.assertRaisesRegex(RuntimeError, "publish compacted control history failed"):
                    compaction.compact_control_history(
                        fixture.control,
                        "agent-control",
                        threshold=3,
                        expected_head=old_head,
                    )

            self.assertTrue(injected)
            self.assertEqual(fixture.remote_head(), concurrent_head[0])
            self.assertTrue((fixture.seed / ".agent" / "status" / "external.json").exists())
            self.assertEqual(fixture.remote_commit_count(), 6)

    def test_lost_push_response_with_immediate_remote_advance_is_reconciled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fixture = ControlRepositoryFixture(Path(tmp))
            old_head = git(fixture.control, "rev-parse", "HEAD")
            original_run_git = admin.run_git
            injected = False
            advanced_head = ""

            def ambiguous_run_git(args, *, cwd=None, timeout=300):
                nonlocal injected, advanced_head
                if args and args[0] == "push" and not injected:
                    injected = True
                    accepted = original_run_git(args, cwd=cwd, timeout=timeout)
                    self.assertEqual(accepted.returncode, 0, accepted.stdout)
                    advanced_head = fixture.advance_rewritten_remote()
                    return subprocess.CompletedProcess(
                        args=["git", *args],
                        returncode=1,
                        stdout="simulated lost push response\n",
                        stderr=None,
                    )
                return original_run_git(args, cwd=cwd, timeout=timeout)

            with mock.patch.object(admin, "run_git", side_effect=ambiguous_run_git):
                result = compaction.compact_control_history(
                    fixture.control,
                    "agent-control",
                    threshold=3,
                    expected_head=old_head,
                )

            self.assertTrue(injected)
            self.assertTrue(result["changed"])
            self.assertTrue(result["push_reconciled"])
            self.assertTrue(result["remote_advanced"])
            self.assertEqual(result["head_sha"], advanced_head)
            self.assertEqual(fixture.remote_head(), advanced_head)
            self.assertEqual(fixture.remote_commit_count(), 2)
            self.assertEqual(git(fixture.control, "rev-parse", "HEAD"), advanced_head)
            self.assertTrue((fixture.control / ".agent/status/post-compact.json").exists())

    def test_backup_bundle_contains_full_control_history_at_expected_head(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fixture = ControlRepositoryFixture(root)
            repository = RepositoryContext(
                repository_id="project-a",
                repository="owner/project-a",
                control=fixture.control,
                work=root / "work",
                checkpoints=root / "checkpoints",
            )
            backup_dir = root / "backups"
            expected_head = fixture.remote_head()

            with mock.patch.object(admin, "clone_url", return_value=str(fixture.remote)):
                bundle = compaction.backup_control_history(
                    repository,
                    backup_dir,
                    expected_head=expected_head,
                )

            self.assertTrue(bundle.is_file())
            heads = git(root, "bundle", "list-heads", str(bundle))
            self.assertIn(expected_head, heads)
            self.assertIn("refs/heads/agent-control", heads)


if __name__ == "__main__":
    unittest.main()
