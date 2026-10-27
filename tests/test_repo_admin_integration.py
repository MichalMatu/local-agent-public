from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import local_agent.repository.admin as admin
from local_agent.repository.context import RepositoryContext


def git(args: list[str], *, cwd: Path | None = None) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )
    if result.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed:\n{result.stdout}")
    return result.stdout


class RepositoryAdminIntegrationTests(unittest.TestCase):
    def test_provision_creates_missing_control_branch_and_two_checkouts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "home"
            home.mkdir()
            (home / ".gitconfig").write_text(
                "[user]\n\tname = local-agent-tests\n\temail = local-agent-tests@example.invalid\n",
                encoding="utf-8",
            )

            remote = root / "remote.git"
            seed = root / "seed"
            git(["init", "--bare", str(remote)])
            git(["init", str(seed)])
            git(["config", "user.name", "local-agent-tests"], cwd=seed)
            git(["config", "user.email", "local-agent-tests@example.invalid"], cwd=seed)
            (seed / "README.md").write_text("# fixture\n", encoding="utf-8")
            git(["add", "README.md"], cwd=seed)
            git(["commit", "-m", "Initial main"], cwd=seed)
            git(["branch", "-M", "main"], cwd=seed)
            git(["remote", "add", "origin", str(remote)], cwd=seed)
            git(["push", "-u", "origin", "main"], cwd=seed)

            normalized_remote = admin.normalize_remote_url(str(remote))
            repository = RepositoryContext(
                repository_id="fixture",
                repository=normalized_remote,
                control=root / "control",
                work=root / "work",
                checkpoints=root / "checkpoints",
            )

            with mock.patch.dict(os.environ, {"HOME": str(home)}), mock.patch.dict(
                admin.core.ENV, {"HOME": str(home)}
            ), mock.patch.object(admin, "clone_url", return_value=str(remote)):
                result = admin.provision_repository(repository)

            self.assertEqual(
                result,
                {
                    "control_created": True,
                    "control_branch_created": True,
                    "work_created": True,
                },
            )
            self.assertTrue((repository.control / ".agent/tasks/.gitkeep").exists())
            self.assertTrue((repository.control / ".agent/results/.gitkeep").exists())
            self.assertTrue(repository.checkpoints.is_dir())
            self.assertIn(
                "refs/heads/agent-control",
                git(["ls-remote", "--heads", str(remote), "refs/heads/agent-control"]),
            )
            self.assertEqual(
                git(["branch", "--show-current"], cwd=repository.control).strip(),
                "agent-control",
            )
            self.assertEqual(
                git(["branch", "--show-current"], cwd=repository.work).strip(),
                "main",
            )


if __name__ == "__main__":
    unittest.main()
