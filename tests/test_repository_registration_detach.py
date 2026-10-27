"""Detached project metadata leaves local source, tasks and recovery files intact."""
from __future__ import annotations

import contextlib
import fcntl
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from local_agent.foundation.process import execution_lease_path
from local_agent.repository import admin, registration_detach
from local_agent.repository.context import (
    load_repository_registry, repository_lease_keys,
)


class RepositoryDetachTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory(prefix="la-detach-test-")
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name)
        self.registry = self.home / "repos.json"
        self.state = self.home / "Library/Application Support/local-agent"
        self.state.mkdir(parents=True)
        for folder in ("claims", "corrupt-claims", "result-spool"):
            (self.state / folder).mkdir()
        (self.state / "agentd.lock").write_text("offline")
        self.work = self.home / "owner-source"
        self.control = self.home / "GitControl"
        self.checkpoints = self.home / "checkpoints"
        for folder in (self.work, self.control, self.checkpoints):
            folder.mkdir()
        (self.work / "unstaged.txt").write_text("DO NOT MODIFY")
        (self.control / "pending-task.json").write_text('{"still": "queued"}')
        (self.checkpoints / "result.patch").write_text("manual recovery")
        self.row = {
            "id": "demo", "repository": "owner/demo", "enabled": False,
            "agent_binding": "b0e94f93-478f-4fc6-abed-159d3702362f",
            "work_dir": str(self.work), "control_dir": str(self.control),
            "checkpoints_dir": str(self.checkpoints),
            "work_mode": "attached", "metadata": {"owner": "preserve"},
        }
        self.other = {
            "id": "second", "repository": "owner/second",
            "enabled": True, "metadata": {"keep": 2},
        }
        self.write([self.row, self.other])

    def write(self, entries: list[dict]) -> None:
        self.registry.write_text(
            json.dumps({"version": 1, "repositories": entries}) + "\n",
        )

    def plan(self) -> dict:
        return registration_detach.plan_repository_detach(
            "demo", home=self.home, registry_path=self.registry,
        )

    def apply(self, expected: str | None = None) -> dict:
        return registration_detach.apply_repository_detach(
            "demo", home=self.home, registry_path=self.registry,
            expected_sha256=expected if expected is not None
            else self.plan()["expected_sha256"],
        )

    def owner_bytes(self) -> tuple[bytes, bytes, bytes]:
        return (
            (self.work / "unstaged.txt").read_bytes(),
            (self.control / "pending-task.json").read_bytes(),
            (self.checkpoints / "result.patch").read_bytes(),
        )

    def add_journal(self, state: str, *, repository: str = "owner/demo") -> Path:
        directory = self.registry.parent / "pending-repository-add"
        directory.mkdir(exist_ok=True)
        path = directory / "demo.json"
        # Transactional add journals store canonical resolved checkout paths.
        context = load_repository_registry(
            home=self.home, path=self.registry, include_disabled=True,
        )[0]
        path.write_text(json.dumps({
            "version": 1, "state": state,
            "repository_id": "demo", "repository": repository,
            "agent_binding": self.row["agent_binding"],
            "work": str(context.work), "control": str(context.control),
            "checkpoints": str(context.checkpoints),
        }))
        return path

    def test_read_only_plan_requires_prior_disable_and_has_no_side_effect(self) -> None:
        before, owner = self.registry.read_bytes(), self.owner_bytes()
        result = self.plan()
        self.assertEqual(result["operation"], "detach_registration_only")
        self.assertFalse(result["changes_applied"])
        self.assertEqual(result["active_claims"], "not_checked")
        self.assertEqual(self.registry.read_bytes(), before)
        self.assertEqual(self.owner_bytes(), owner)
        self.write([{**self.row, "enabled": True}, self.other])
        with self.assertRaisesRegex(ValueError, "disable repository"):
            self.plan()
        with self.assertRaisesRegex(ValueError, "disable repository"):
            self.apply(expected=self.plan_digest_for_active())

    def plan_digest_for_active(self) -> str:
        import hashlib
        return hashlib.sha256(self.registry.read_bytes()).hexdigest()

    def test_detach_preserves_user_files_control_mailbox_and_other_entries(self) -> None:
        before = self.owner_bytes()
        result = self.apply()
        self.assertEqual(result["status"], "detached")
        self.assertTrue(result["registry_only"])
        self.assertTrue(result["binding_catalog_unchanged"])
        rows = json.loads(self.registry.read_text())["repositories"]
        self.assertEqual(rows, [self.other])
        self.assertEqual(self.owner_bytes(), before)
        self.assertTrue((self.control / "pending-task.json").exists())
        self.assertEqual(
            [p.repository_id for p in load_repository_registry(
                path=self.registry, home=self.home, allow_empty=True,
            )], ["second"],
        )

    def test_detach_last_disabled_project_leaves_valid_zero_project_registry(self) -> None:
        self.write([self.row])
        self.assertEqual(self.apply()["status"], "detached")
        self.assertEqual(json.loads(self.registry.read_text())["repositories"], [])
        self.assertEqual(
            load_repository_registry(
                home=self.home, path=self.registry, allow_empty=True,
            ), [],
        )

    def test_pending_add_journal_blocks_until_committed(self) -> None:
        before, owner = self.registry.read_bytes(), self.owner_bytes()
        journal = self.add_journal("prepared")
        with self.assertRaisesRegex(RuntimeError, "unreconciled repository-add journal"):
            self.apply()
        self.assertEqual(self.registry.read_bytes(), before)
        self.assertEqual(self.owner_bytes(), owner)
        journal.write_text(journal.read_text().replace("prepared", "committed"))
        self.assertEqual(self.apply()["status"], "detached")
        self.assertTrue(journal.exists())

    def test_mismatched_or_symlinked_journal_rejected_without_worktree_changes(self) -> None:
        before = self.registry.read_bytes()
        journal = self.add_journal("committed", repository="owner/attacker")
        with self.assertRaisesRegex(RuntimeError, "journal identity mismatch"):
            self.apply()
        journal.unlink()
        secret = self.home / "secret.json"
        secret.write_text("{}")
        journal.symlink_to(secret)
        with self.assertRaisesRegex(RuntimeError, "journal path is not trusted"):
            self.apply()
        self.assertEqual(secret.read_text(), "{}")
        self.assertEqual(self.registry.read_bytes(), before)

    def test_active_daemon_or_orphaned_repository_lease_blocks(self) -> None:
        before = self.registry.read_bytes()
        with (self.state / "agentd.lock").open("r+") as held:
            fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaisesRegex(RuntimeError, "daemon is active"):
                self.apply()
        context = load_repository_registry(
            home=self.home, path=self.registry, include_disabled=True,
        )[0]
        lockdir = self.state / "locks/repositories"
        lockdir.mkdir(parents=True)
        path = execution_lease_path(lockdir, repository_lease_keys(context)[0])
        with path.open("a+") as held:
            fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaisesRegex(RuntimeError, "execution lease is busy"):
                self.apply()
        self.assertEqual(self.registry.read_bytes(), before)

    def test_durable_claim_and_published_result_spool_refuse_detach(self) -> None:
        before = self.registry.read_bytes()
        for location in ("claims", "corrupt-claims", "result-spool", "host-tasks/claims"):
            with self.subTest(location=location):
                folder = self.state / location
                folder.mkdir(parents=True, exist_ok=True)
                record = folder / "unknown.json"
                record.write_text('{"status":"unknown"}')
                with self.assertRaisesRegex(RuntimeError, "unresolved durable"):
                    self.apply()
                record.unlink()
        self.assertEqual(self.registry.read_bytes(), before)

    def test_stale_sha_and_wrong_repository_refuse_detach(self) -> None:
        original = self.plan()["expected_sha256"]
        self.write([self.row])
        before = self.registry.read_bytes()
        with self.assertRaisesRegex(RuntimeError, "changed since detach plan"):
            self.apply(original)
        with self.assertRaisesRegex(ValueError, "exact SHA256"):
            self.apply("bad")
        with self.assertRaisesRegex(ValueError, "not registered"):
            registration_detach.plan_repository_detach(
                "missing", home=self.home, registry_path=self.registry,
            )
        self.assertEqual(self.registry.read_bytes(), before)

    def test_atomic_write_failure_preserves_registry_and_retry_releases_all_locks(self) -> None:
        digest, original, owner = (
            self.plan()["expected_sha256"], self.registry.read_bytes(), self.owner_bytes(),
        )
        with mock.patch.object(registration_detach, "_atomic_json", side_effect=OSError("disk full")):
            with self.assertRaisesRegex(OSError, "disk full"):
                self.apply(digest)
        self.assertEqual(self.registry.read_bytes(), original)
        self.assertEqual(self.owner_bytes(), owner)
        self.assertEqual(self.apply(digest)["status"], "detached")

    def test_registry_only_cli_plan_and_explicit_apply(self) -> None:
        argv = ["admin.py", "--registry", str(self.registry), "detach", "--repository-id", "demo"]
        out = io.StringIO()
        with mock.patch("sys.argv", argv), mock.patch.object(
            registration_detach.Path, "home", return_value=self.home,
        ), contextlib.redirect_stdout(out):
            self.assertEqual(admin.main(), 0)
        plan = json.loads(out.getvalue())
        self.assertFalse(plan["changes_applied"])
        out = io.StringIO()
        with mock.patch("sys.argv", argv + ["--apply"]), mock.patch.object(
            registration_detach.Path, "home", return_value=self.home,
        ), contextlib.redirect_stdout(out):
            self.assertEqual(admin.main(), 2)
        self.assertEqual(json.loads(out.getvalue())["status"], "unverified")
        out = io.StringIO()
        with mock.patch("sys.argv", argv + [
            "--expected-sha256", plan["expected_sha256"], "--apply",
        ]), mock.patch.object(
            registration_detach.Path, "home", return_value=self.home,
        ), contextlib.redirect_stdout(out):
            self.assertEqual(admin.main(), 0)
        self.assertEqual(json.loads(out.getvalue())["status"], "detached")


if __name__ == "__main__":
    unittest.main()
