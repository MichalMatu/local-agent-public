from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import local_agent.repository.admin as admin
import local_agent.foundation.storage as storage
from local_agent.repository.context import RepositoryContext


def repository(root: Path) -> RepositoryContext:
    return RepositoryContext(
        repository_id="project-a",
        repository="owner/project-a",
        control=root / "control",
        work=root / "work",
        checkpoints=root / "checkpoints",
    )


def git_ok(args: list[str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=args or ["git"], returncode=0, stdout="")


class RepositoryAdminTests(unittest.TestCase):
    def test_normalize_remote_url_accepts_common_github_forms(self) -> None:
        expected = "owner/project-a"
        self.assertEqual(admin.normalize_remote_url("https://github.com/owner/project-a.git"), expected)
        self.assertEqual(admin.normalize_remote_url("git@github.com:owner/project-a.git"), expected)
        self.assertEqual(admin.normalize_remote_url("ssh://git@github.com/owner/project-a.git"), expected)
        self.assertEqual(
            admin.normalize_remote_url("ssh://git@ssh.github.com:443/owner/project-a.git"),
            expected,
        )
        self.assertEqual(
            admin.normalize_remote_url("ssh://git@ssh.github.com/owner/project-a.git"),
            expected,
        )

    def test_validate_checkout_rejects_wrong_origin(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = repository(Path(tmp))
            (repo.control / ".git").mkdir(parents=True)
            result = subprocess.CompletedProcess(
                args=["git"],
                returncode=0,
                stdout="https://github.com/owner/other.git\n",
            )
            with mock.patch.object(admin, "run_git", return_value=result):
                with self.assertRaisesRegex(RuntimeError, "origin mismatch"):
                    admin.validate_checkout(repo.control, repo, "control")

    def test_validate_checkout_accepts_case_insensitive_origin_identity(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = repository(Path(tmp))
            (repo.control / ".git").mkdir(parents=True)
            result = subprocess.CompletedProcess(
                args=["git"],
                returncode=0,
                stdout="https://github.com/OWNER/PROJECT-A.git\n",
            )
            with mock.patch.object(admin, "run_git", return_value=result):
                admin.validate_checkout(repo.control, repo, "control")

    def test_control_clone_is_shallow_partial_sparse_and_tagless(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = repository(Path(tmp))
            with mock.patch.object(
                admin,
                "run_git",
                side_effect=[git_ok(), git_ok()],
            ) as run_git, mock.patch.object(admin, "validate_checkout"):
                created = admin._clone_if_missing(
                    repo,
                    path=repo.control,
                    branch=repo.control_branch,
                    single_branch=True,
                    label="control",
                    shallow_depth=storage.CONTROL_HISTORY_DEPTH,
                    partial_clone=True,
                    sparse_paths=storage.CONTROL_SPARSE_PATHS,
                    no_tags=True,
                )

            self.assertTrue(created)
            clone_args = run_git.call_args_list[0].args[0]
            self.assertEqual(
                clone_args,
                [
                    "clone",
                    "--branch",
                    "agent-control",
                    "--single-branch",
                    "--depth",
                    "256",
                    "--filter=blob:none",
                    "--sparse",
                    "--no-tags",
                    "https://github.com/owner/project-a.git",
                    str(repo.control),
                ],
            )
            self.assertEqual(
                run_git.call_args_list[1].args[0],
                ["sparse-checkout", "set", ".agent"],
            )

    def test_provision_uses_existing_remote_control_branch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = repository(Path(tmp))
            with mock.patch.object(
                admin, "remote_branch_exists", return_value=True
            ), mock.patch.object(
                admin,
                "_clone_if_missing",
                side_effect=[True, True],
            ) as clone, mock.patch.object(admin, "validate_repository"):
                result = admin.provision_repository(repo)

            self.assertEqual(
                result,
                {
                    "control_created": True,
                    "control_branch_created": False,
                    "work_created": True,
                },
            )
            self.assertTrue(repo.checkpoints.is_dir())
            self.assertEqual(clone.call_count, 2)
            control_call = clone.call_args_list[0].kwargs
            self.assertTrue(control_call["single_branch"])
            self.assertEqual(control_call["shallow_depth"], storage.CONTROL_HISTORY_DEPTH)
            self.assertTrue(control_call["partial_clone"])
            self.assertEqual(control_call["sparse_paths"], storage.CONTROL_SPARSE_PATHS)
            self.assertTrue(control_call["no_tags"])
            self.assertFalse(clone.call_args_list[1].kwargs["single_branch"])

    def test_provision_initializes_missing_remote_control_branch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = repository(Path(tmp))
            with mock.patch.object(
                admin, "remote_branch_exists", return_value=False
            ), mock.patch.object(
                admin,
                "_clone_if_missing",
                side_effect=[True, True],
            ) as clone, mock.patch.object(
                admin, "initialize_control_branch"
            ) as initialize, mock.patch.object(admin, "validate_repository"):
                result = admin.provision_repository(repo)

            self.assertEqual(
                result,
                {
                    "control_created": True,
                    "control_branch_created": True,
                    "work_created": True,
                },
            )
            initialize.assert_called_once_with(repo)
            control_call = clone.call_args_list[0].kwargs
            self.assertTrue(control_call["single_branch"])
            self.assertEqual(control_call["branch"], "main")
            self.assertEqual(control_call["shallow_depth"], 1)
            self.assertTrue(control_call["partial_clone"])
            self.assertEqual(control_call["sparse_paths"], storage.CONTROL_SPARSE_PATHS)
            self.assertTrue(control_call["no_tags"])

    def test_existing_non_git_destination_is_never_overwritten(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = repository(Path(tmp))
            repo.control.mkdir(parents=True)
            (repo.control / "keep.txt").write_text("keep", encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "checkout missing"):
                admin._clone_if_missing(
                    repo,
                    path=repo.control,
                    branch=repo.control_branch,
                    single_branch=True,
                    label="control",
                )
            self.assertEqual((repo.control / "keep.txt").read_text(encoding="utf-8"), "keep")

    def test_select_repository_rejects_unknown_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = repository(Path(tmp))
            with self.assertRaisesRegex(ValueError, "not enabled"):
                admin.select_repositories([repo], "missing")


if __name__ == "__main__":
    unittest.main()
