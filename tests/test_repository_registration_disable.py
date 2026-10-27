"""Guarded offline disable may only alter one registry flag under trusted locks."""
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
from local_agent.repository import admin, registration_disable
from local_agent.repository.context import (
    load_repository_registry, repository_lease_keys,
)


class RepositoryDisableTests(unittest.TestCase):
    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory(prefix="la-disable-test-")
        self.addCleanup(temp.cleanup)
        self.home = Path(temp.name)
        self.registry = self.home / "registrations.json"
        self.state = self.home / "Library/Application Support/local-agent"
        self.state.mkdir(parents=True)
        for name in ("claims", "corrupt-claims", "result-spool"):
            (self.state / name).mkdir()
        (self.state / "agentd.lock").write_text("test daemon offline\n")
        self.owner = self.home / "dirty-user-project"
        self.owner.mkdir()
        (self.owner / "working.txt").write_text("DO NOT TOUCH\n")
        self.control = self.home / "control-checkout"
        self.control.mkdir()
        (self.control / "keep.json").write_text('{"task": "unpublished"}')
        self.checkpoints = self.home / "checkpoints"
        self.checkpoints.mkdir()
        (self.checkpoints / "recovery.txt").write_text("unfinished")
        self.original = self.row()
        self.write([self.original])

    def row(self, repo_id: str = "first", **options: object) -> dict:
        item = {
            "id": repo_id,
            "repository": f"owner/{repo_id}",
            "enabled": True,
            "work_dir": str(self.owner if repo_id == "first" else self.home / repo_id / "work"),
            "control_dir": str(self.control if repo_id == "first" else self.home / repo_id / "control"),
            "checkpoints_dir": str(self.checkpoints if repo_id == "first" else self.home / repo_id / "checkpoints"),
            "work_mode": "attached",
            "agent_binding": "b0e94f93-478f-4fc6-abed-159d3702362f"
            if repo_id == "first" else "fe99a852-0486-4b3a-b09b-5d295e9355a8",
            "custom_option": {"preserve": True},
        }
        item.update(options)
        return item

    def write(self, entries: list[dict]) -> None:
        self.registry.write_text(json.dumps({"version": 1, "repositories": entries}) + "\n")

    def plan(self) -> dict:
        return registration_disable.plan_repository_disable(
            "first", home=self.home, registry_path=self.registry,
        )

    def apply(self, digest: str | None = None) -> dict:
        return registration_disable.apply_repository_disable(
            "first", home=self.home, registry_path=self.registry,
            expected_sha256=digest if digest is not None else self.plan()["expected_sha256"],
        )

    def owner_snapshot(self) -> tuple[bytes, bytes, bytes]:
        return (
            (self.owner / "working.txt").read_bytes(),
            (self.control / "keep.json").read_bytes(),
            (self.checkpoints / "recovery.txt").read_bytes(),
        )

    def test_plan_has_no_effects_and_never_claims_quiescence(self) -> None:
        before, owner = self.registry.read_bytes(), self.owner_snapshot()
        plan = self.plan()
        self.assertEqual(plan["status"], "planned_only")
        self.assertEqual(len(plan["expected_sha256"]), 64)
        self.assertEqual(plan["active_claims"], "not_checked")
        self.assertEqual(plan["active_leases"], "not_checked")
        self.assertFalse(plan["changes_applied"])
        self.assertEqual(self.registry.read_bytes(), before)
        self.assertEqual(self.owner_snapshot(), owner)

    def test_offline_atomic_disable_preserves_every_workspace_and_unknown_keys(self) -> None:
        second = self.row("second")
        self.write([self.original, second])
        owner = self.owner_snapshot()
        result = self.apply()
        self.assertEqual(result["status"], "disabled")
        self.assertTrue(result["reload_required"])
        self.assertTrue(result["workspaces_preserved"])
        entries = json.loads(self.registry.read_text())["repositories"]
        self.assertFalse(entries[0]["enabled"])
        self.assertTrue(entries[1]["enabled"])
        self.assertEqual(entries[0]["custom_option"], {"preserve": True})
        self.assertEqual(self.owner_snapshot(), owner)
        self.assertEqual(
            [repo.repository_id for repo in load_repository_registry(
                home=self.home, path=self.registry, allow_empty=True,
            )], ["second"],
        )
        unchanged = self.registry.read_bytes()
        self.assertEqual(self.apply()["status"], "already_disabled")
        self.assertEqual(self.registry.read_bytes(), unchanged)

    def test_daemon_lock_busy_prevents_write_even_when_claims_empty(self) -> None:
        original = self.registry.read_bytes()
        with (self.state / "agentd.lock").open("r+") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaisesRegex(RuntimeError, "daemon is active"):
                self.apply()
        self.assertEqual(self.registry.read_bytes(), original)

    def test_missing_or_symlink_daemon_lock_fails_closed(self) -> None:
        lock = self.state / "agentd.lock"
        lock.unlink()
        with self.assertRaisesRegex(RuntimeError, "lock is unavailable"):
            self.apply()
        destination = self.home / "false-lock"
        destination.write_text("not a daemon lock")
        lock.symlink_to(destination)
        with self.assertRaisesRegex(RuntimeError, "lock is unavailable"):
            self.apply()

    def test_pending_claim_corruption_spool_or_host_claim_blocks(self) -> None:
        baseline = self.registry.read_bytes()
        locations = ("claims", "corrupt-claims", "result-spool", "host-tasks/claims")
        for name in locations:
            with self.subTest(name=name):
                directory = self.state / name
                directory.mkdir(parents=True, exist_ok=True)
                pending = directory / "unknown.json"
                pending.write_text("{not safe")
                with self.assertRaisesRegex(RuntimeError, "unresolved durable"):
                    self.apply()
                pending.unlink()
        self.assertEqual(self.registry.read_bytes(), baseline)

    def test_completed_host_claim_history_does_not_block_offline_disable(self) -> None:
        claimdir = self.state / "host-tasks" / "claims"
        resultdir = self.state / "host-tasks" / "results"
        claimdir.mkdir(parents=True)
        resultdir.mkdir(parents=True)
        for i, status in enumerate(("done", "failed")):
            identity = f"host-{i}"
            content = {"id": identity, "status": "claimed",
                       "task_digest": "a" * 64,
                       "target_host_id": "example-mac",
                       "target_installation_id": "install"}
            (claimdir / f"{identity}.json").write_text(json.dumps(content))
            (resultdir / f"{identity}.json").write_text(json.dumps({
                **content, "status": status, "finished_at": "2026-10-19T09:00:00Z",
            }))
        before = self.owner_snapshot()
        result = self.apply()
        self.assertEqual(result["status"], "disabled")
        self.assertEqual(self.owner_snapshot(), before)
        self.assertEqual(len(list(claimdir.iterdir())), 2)
        self.assertEqual(len(list(resultdir.iterdir())), 2)

    def test_host_claim_without_matching_terminal_result_blocks_without_mutation(self) -> None:
        claimdir = self.state / "host-tasks" / "claims"
        resultdir = self.state / "host-tasks" / "results"
        claimdir.mkdir(parents=True)
        resultdir.mkdir(parents=True)
        item = {"id": "active", "status": "claimed", "task_digest": "a" * 64,
                "target_host_id": "example-mac",
                "target_installation_id": "install"}
        (claimdir / "active.json").write_text(json.dumps(item))
        initial = self.registry.read_bytes()
        with self.assertRaisesRegex(RuntimeError, "unresolved durable host claims"):
            self.apply()
        bad = {**item, "status": "running"}
        (resultdir / "active.json").write_text(json.dumps(bad))
        with self.assertRaisesRegex(RuntimeError, "result mismatch"):
            self.apply()
        bad["status"] = "done"
        bad["task_digest"] = "b" * 64
        (resultdir / "active.json").write_text(json.dumps(bad))
        with self.assertRaisesRegex(RuntimeError, "result mismatch"):
            self.apply()
        (resultdir / "active.json").unlink()
        (resultdir / "active.json").symlink_to(self.owner / "working.txt")
        with self.assertRaisesRegex(RuntimeError, "missing terminal result"):
            self.apply()
        self.assertEqual(self.registry.read_bytes(), initial)
        self.assertEqual((self.owner / "working.txt").read_text(), "DO NOT TOUCH\n")

    def test_unknown_or_symlink_claim_state_fails_closed(self) -> None:
        path = self.state / "claims"
        path.rmdir()
        with self.assertRaisesRegex(RuntimeError, "state cannot be verified"):
            self.apply()
        other = self.home / "redirected"
        other.mkdir()
        path.symlink_to(other, target_is_directory=True)
        with self.assertRaisesRegex(RuntimeError, "state cannot be verified"):
            self.apply()

    def test_repository_lease_busy_blocks_even_when_daemon_lock_is_free(self) -> None:
        context = load_repository_registry(home=self.home, path=self.registry)[0]
        lock_dir = self.state / "locks" / "repositories"
        lock_dir.mkdir(parents=True)
        path = execution_lease_path(lock_dir, repository_lease_keys(context)[0])
        with path.open("a+") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaisesRegex(RuntimeError, "execution lease is busy"):
                self.apply()
        self.assertTrue(json.loads(self.registry.read_text())["repositories"][0]["enabled"])

    def test_symlink_lease_file_is_rejected(self) -> None:
        context = load_repository_registry(home=self.home, path=self.registry)[0]
        lock_dir = self.state / "locks" / "repositories"
        lock_dir.mkdir(parents=True)
        path = execution_lease_path(lock_dir, sorted(repository_lease_keys(context))[0])
        dest = self.home / "secret"
        dest.write_text("NO")
        path.symlink_to(dest)
        with self.assertRaisesRegex(RuntimeError, "lease path must not be a symlink"):
            self.apply()
        self.assertEqual(dest.read_text(), "NO")

    def test_digest_stale_or_invalid_blocks_without_mutation(self) -> None:
        old = self.plan()["expected_sha256"]
        self.write([self.original, self.row("second")])
        before = self.registry.read_bytes()
        with self.assertRaisesRegex(RuntimeError, "changed since disable plan"):
            self.apply(old)
        with self.assertRaisesRegex(ValueError, "exact SHA256"):
            self.apply("invalid")
        self.assertEqual(self.registry.read_bytes(), before)

    def test_invalid_registry_and_wrong_id_fail_closed(self) -> None:
        self.write([self.original, self.row("first", enabled=False)])
        with self.assertRaisesRegex(ValueError, "duplicate repository id"):
            self.plan()
        with self.assertRaisesRegex(ValueError, "duplicate repository id"):
            self.apply("a" * 64)
        self.write([self.original])
        with self.assertRaisesRegex(ValueError, "not registered"):
            registration_disable.plan_repository_disable(
                "missing", home=self.home, registry_path=self.registry,
            )

    def test_atomic_write_failure_rolls_back_registry_and_releases_locks(self) -> None:
        before = self.registry.read_bytes()
        digest = self.plan()["expected_sha256"]
        with mock.patch.object(registration_disable, "_atomic_json", side_effect=OSError("disk full")):
            with self.assertRaisesRegex(OSError, "disk full"):
                self.apply(digest)
        self.assertEqual(self.registry.read_bytes(), before)
        self.assertEqual(self.apply(digest)["status"], "disabled")

    def test_cli_plan_is_readonly_and_apply_requires_matching_sha(self) -> None:
        argv = [
            "admin.py", "--registry", str(self.registry),
            "disable", "--repository-id", "first",
        ]
        buf = io.StringIO()
        with mock.patch("sys.argv", argv), mock.patch.object(
            registration_disable.Path, "home", return_value=self.home,
        ), contextlib.redirect_stdout(buf):
            self.assertEqual(admin.main(), 0)
        plan = json.loads(buf.getvalue())
        self.assertEqual(plan["status"], "planned_only")
        buf = io.StringIO()
        with mock.patch("sys.argv", argv + ["--apply"]), mock.patch.object(
            registration_disable.Path, "home", return_value=self.home,
        ), contextlib.redirect_stdout(buf):
            self.assertEqual(admin.main(), 2)
        self.assertFalse(json.loads(buf.getvalue())["changes_applied"])
        buf = io.StringIO()
        with mock.patch("sys.argv", argv + [
            "--expected-sha256", plan["expected_sha256"], "--apply",
        ]), mock.patch.object(
            registration_disable.Path, "home", return_value=self.home,
        ), contextlib.redirect_stdout(buf):
            self.assertEqual(admin.main(), 0)
        self.assertEqual(json.loads(buf.getvalue())["status"], "disabled")


if __name__ == "__main__":
    unittest.main()
