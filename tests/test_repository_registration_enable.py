"""Re-enable one disabled registration only with daemon offline and exact SHA."""
from __future__ import annotations

import fcntl
import json
from pathlib import Path
import tempfile
import unittest

from local_agent.repository.registration_enable import (
    apply_repository_enable, plan_repository_enable,
)
from local_agent.repository.context import load_repository_registry


class RepositoryEnableTests(unittest.TestCase):
    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory(prefix="la-enable-registry-")
        self.addCleanup(temp.cleanup)
        self.home = Path(temp.name)
        self.registry = self.home / "repositories.json"
        self.state = self.home / "Library/Application Support/local-agent"
        self.state.mkdir(parents=True)
        for name in ("claims", "corrupt-claims", "result-spool"):
            (self.state / name).mkdir()
        (self.state / "agentd.lock").write_text("offline\n")
        self.owner = self.home / "owner-dirty"
        self.owner.mkdir()
        (self.owner / "working.txt").write_text("keep staged and untracked work\n")
        self.control = self.home / "control"
        self.control.mkdir()
        (self.control / "queued-task.json").write_text('{"id":"old"}')
        self.checkpoints = self.home / "checkpoints"
        self.checkpoints.mkdir()
        (self.checkpoints / "history").write_text("keep")
        self.registry.write_text(json.dumps({"version": 1, "repositories": [{
            "id": "first", "repository": "owner/first", "enabled": False,
            "work_dir": str(self.owner), "control_dir": str(self.control),
            "checkpoints_dir": str(self.checkpoints), "work_mode": "attached",
            "agent_binding": "b0e94f93-478f-4fc6-abed-159d3702362f",
            "custom": {"keep": True},
        }]}) + "\n")

    def plan(self):
        return plan_repository_enable("first", registry_path=self.registry, home=self.home)

    def apply(self, sha=None):
        return apply_repository_enable(
            "first", registry_path=self.registry, home=self.home,
            expected_sha256=sha or self.plan()["expected_sha256"],
        )

    def test_plan_only_has_no_effects(self) -> None:
        old = self.registry.read_bytes()
        result = self.plan()
        self.assertEqual(result["status"], "planned_only")
        self.assertFalse(result["already_enabled"])
        self.assertEqual(result["control_branch"], "agent-control")
        self.assertEqual(self.registry.read_bytes(), old)

    def test_enable_only_changes_one_flag_and_preserves_owner(self) -> None:
        result = self.apply()
        self.assertEqual(result["status"], "enabled")
        self.assertTrue(result["reload_required"])
        row = json.loads(self.registry.read_text())["repositories"][0]
        self.assertTrue(row["enabled"])
        self.assertEqual(row["custom"], {"keep": True})
        self.assertEqual((self.owner / "working.txt").read_text(),
                         "keep staged and untracked work\n")
        self.assertEqual((self.control / "queued-task.json").read_text(),
                         '{"id":"old"}')
        self.assertEqual((self.checkpoints / "history").read_text(), "keep")
        self.assertEqual([x.repository_id for x in load_repository_registry(
            home=self.home, path=self.registry)], ["first"])
        before = self.registry.read_bytes()
        self.assertEqual(self.apply()["status"], "already_enabled")
        self.assertEqual(self.registry.read_bytes(), before)

    def test_daemon_running_fails_without_registry_mutation(self) -> None:
        original = self.registry.read_bytes()
        with (self.state / "agentd.lock").open("r+") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaisesRegex(RuntimeError, "daemon is active"):
                self.apply()
        self.assertEqual(self.registry.read_bytes(), original)

    def test_pending_host_or_global_claim_blocks(self) -> None:
        original = self.registry.read_bytes()
        for sub in ("claims", "result-spool", "host-tasks/claims"):
            folder = self.state / sub
            folder.mkdir(parents=True, exist_ok=True)
            marker = folder / "unfinished.json"
            marker.write_text("unknown")
            with self.assertRaisesRegex(RuntimeError, "unresolved durable"):
                self.apply()
            marker.unlink()
        self.assertEqual(self.registry.read_bytes(), original)

    def test_registry_stale_sha_or_bad_input_fails_closed(self) -> None:
        old = self.plan()["expected_sha256"]
        self.registry.write_text(self.registry.read_text() + " ")
        updated = self.registry.read_bytes()
        with self.assertRaisesRegex(RuntimeError, "changed since enable plan"):
            self.apply(old)
        with self.assertRaisesRegex(ValueError, "exact SHA256"):
            self.apply("invalid")
        self.assertEqual(self.registry.read_bytes(), updated)

    def test_admin_cli_plan_and_explicit_sha_apply(self) -> None:
        from contextlib import redirect_stdout
        from unittest import mock
        import io

        from local_agent.repository import admin
        from local_agent.repository import registration_enable as module

        prefix = ["admin.py", "--registry", str(self.registry),
                  "enable", "--repository-id", "first"]
        before = self.registry.read_bytes()
        output = io.StringIO()
        with (mock.patch("sys.argv", prefix),
              mock.patch.object(module.Path, "home", return_value=self.home),
              redirect_stdout(output)):
            self.assertEqual(admin.main(), 0)
        plan = json.loads(output.getvalue())
        self.assertEqual(plan["status"], "planned_only")
        self.assertEqual(self.registry.read_bytes(), before)
        output = io.StringIO()
        with (mock.patch("sys.argv", prefix + ["--apply",
              "--expected-sha256", plan["expected_sha256"]]),
              mock.patch.object(module.Path, "home", return_value=self.home),
              redirect_stdout(output)):
            self.assertEqual(admin.main(), 0)
        self.assertEqual(json.loads(output.getvalue())["status"], "enabled")

    def test_missing_project_cannot_be_invented(self) -> None:
        with self.assertRaisesRegex(ValueError, "not registered"):
            plan_repository_enable("missing", registry_path=self.registry, home=self.home)
        with self.assertRaisesRegex(ValueError, "not registered"):
            apply_repository_enable("missing", expected_sha256=self.plan()["expected_sha256"],
                                    registry_path=self.registry, home=self.home)


if __name__ == "__main__":
    unittest.main()
