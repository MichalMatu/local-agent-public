"""Offline preflight of custom / existing project checkout onboarding."""
from __future__ import annotations

import contextlib
import io
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from local_agent.repository import admin
from local_agent.repository.onboarding import plan_repository_add


class ProjectOnboardingPlanTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="local-agent-add-plan-")
        self.addCleanup(self.tmp.cleanup)
        self.home = (Path(self.tmp.name) / "home").resolve(strict=False)
        self.home.mkdir()
        self.catalog = self.home / "catalog.json"
        self.catalog.write_text(
            json.dumps({"version": 1, "agents": [{
                "id": "example-project", "repository": "example-owner/example-project",
                "agent_binding": "fe99a852-0486-4b3a-b09b-5d295e9355a8",
                "execution_enabled": True,
            }]}),
            encoding="utf-8",
        )
        self.registry = self.home / "Library/Application Support/local-agent/repositories.json"

    def plan(self, repository="example-owner/example-project", **kwargs):
        return plan_repository_add(
            repository, home=self.home, cwd=self.home,
            catalog_path=self.catalog, **kwargs,
        )

    @staticmethod
    def git(args, cwd):
        result = subprocess.run(
            ["git", *args], cwd=cwd, capture_output=True, text=True, timeout=10,
        )
        if result.returncode:
            raise AssertionError(result.stderr)
        return result.stdout

    def init_repo(self, path, remote="https://github.com/example-owner/example-project.git"):
        path.mkdir(parents=True)
        self.git(["init", "-q"], path)
        self.git(["remote", "add", "origin", remote], path)

    def test_default_separate_workspace_is_planned_without_writes(self):
        plan = self.plan()
        self.assertEqual(plan["status"], "planned_only")
        self.assertEqual(plan["work"]["action"], "clone_if_missing")
        self.assertEqual(plan["control"]["action"], "clone_if_missing")
        self.assertEqual(plan["binding_action"], "reuse_canonical_binding")
        self.assertEqual(plan["execution_authority"], "not_asserted")
        self.assertFalse(plan["changes_applied"])
        self.assertFalse(plan["execution_ready"])
        self.assertFalse(plan["existing_worktree_requires_safe_executor"])
        self.assertEqual(plan["work"]["path"],
                         str(self.home / "agent-workspace/repos/example-project/work"))
        self.assertFalse(self.registry.exists())
        self.assertFalse((self.home / "agent-workspace").exists())

    def test_existing_dirty_clone_reused_without_reset_or_second_copy(self):
        work = self.home / "Projects" / "example-project"
        self.init_repo(work)
        (work / "do-not-touch.txt").write_text("UNCOMMITTED", encoding="utf-8")
        before = (work / "do-not-touch.txt").read_bytes()
        with mock.patch.object(admin, "remote_branch_exists",
                               side_effect=AssertionError("network forbidden")), \
             mock.patch.object(admin, "provision_repository",
                               side_effect=AssertionError("provision forbidden")):
            plan = self.plan(work_dir=work)
        self.assertEqual(plan["work"]["action"], "reuse_existing_checkout")
        self.assertTrue(plan["existing_worktree_requires_safe_executor"])
        self.assertFalse(plan["execution_ready"])
        self.assertEqual(plan["control"]["action"], "clone_if_missing")
        self.assertNotEqual(plan["work"]["path"], plan["control"]["path"])
        self.assertEqual((work / "do-not-touch.txt").read_bytes(), before)
        self.assertFalse(self.registry.exists())

    def test_can_choose_absolute_control_and_checkpoints_and_relative_work(self):
        work = self.home / "Work" / "src"
        self.init_repo(work)
        plan = self.plan(
            work_dir=Path("Work/src"),
            control_dir=self.home / "service-control",
            checkpoints_dir=self.home / "checkpoint-vault",
        )
        self.assertEqual(plan["work"]["path"], str(work))
        self.assertEqual(plan["control"]["path"], str(self.home / "service-control"))
        self.assertEqual(plan["checkpoints"]["path"], str(self.home / "checkpoint-vault"))
        self.assertFalse((self.home / "service-control").exists())

    def test_wrong_git_origin_is_rejected_and_work_is_unchanged(self):
        work = self.home / "Projects" / "Other"
        self.init_repo(work, "https://github.com/example-owner/other.git")
        (work / "keep.txt").write_text("untouched")
        with self.assertRaisesRegex(RuntimeError, "origin mismatch"):
            self.plan(work_dir=work)
        self.assertEqual((work / "keep.txt").read_text(), "untouched")

    def test_existing_non_git_folder_is_never_clobbered(self):
        work = self.home / "Projects" / "example-project"
        work.mkdir(parents=True)
        (work / "file.txt").write_text("keep")
        with self.assertRaisesRegex(RuntimeError, "checkout missing"):
            self.plan(work_dir=work)
        self.assertEqual((work / "file.txt").read_text(), "keep")

    def test_collision_with_control_or_other_repository_is_rejected(self):
        work = self.home / "Projects" / "example-project"
        self.init_repo(work)
        with self.assertRaisesRegex(ValueError, "workspace path collision"):
            self.plan(work_dir=work, control_dir=work / "agent-control")
        self.registry.parent.mkdir(parents=True)
        self.registry.write_text(json.dumps({
            "version": 1, "repositories": [{
                "id": "different", "repository": "Owner/other",
                "work_dir": str(work),
            }],
        }))
        with self.assertRaisesRegex(ValueError, "workspace path collision"):
            self.plan(work_dir=work)

    def test_registered_remote_is_rejected_even_if_disabled(self):
        self.registry.parent.mkdir(parents=True)
        self.registry.write_text(json.dumps({
            "version": 1, "repositories": [{
                "id": "already", "repository": "example-owner/example-project",
                "enabled": False,
            }],
        }))
        with self.assertRaisesRegex(ValueError, "duplicate remote repository"):
            self.plan()

    def test_unregistered_catalog_binding_is_not_silently_invented(self):
        plan = self.plan("Owner/NewProject")
        self.assertEqual(plan["binding_action"], "binding_onboarding_required")
        self.assertIsNone(plan["catalog_binding"])
        self.assertFalse(plan["changes_applied"])

    def test_bad_repository_and_explicit_missing_registry_fail_closed(self):
        for remote in ("not-a-repo", "Owner/..", "Owner/.", "../repo", "./repo"):
            with self.subTest(remote=remote):
                with self.assertRaisesRegex(ValueError, "invalid repository"):
                    self.plan(remote)
        self.assertFalse((self.home / "agent-workspace").exists())
        with self.assertRaisesRegex(ValueError, "explicit registry"):
            self.plan(registry_path=self.registry)

    def test_cli_plan_add_json_is_read_only(self):
        work = self.home / "Projects/example-project"
        self.init_repo(work)
        argv = [
            "admin.py", "plan-add", "example-owner/example-project",
            "--work-dir", str(work), "--catalog", str(self.catalog),
        ]
        buffer = io.StringIO()
        with mock.patch("sys.argv", argv), mock.patch(
            "local_agent.repository.onboarding.Path.home", return_value=self.home,
        ), contextlib.redirect_stdout(buffer):
            self.assertEqual(admin.main(), 0)
        result = json.loads(buffer.getvalue())
        self.assertEqual(result["work"]["action"], "reuse_existing_checkout")
        self.assertFalse(result["changes_applied"])
        self.assertFalse(self.registry.exists())


if __name__ == "__main__":
    unittest.main()
