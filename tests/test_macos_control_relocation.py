"""Real local Git/lease tests; launchd and network effects are never used."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

from local_agent.foundation.process import ExecutionLeaseBusy, acquire_execution_leases
from local_agent.repository.host_routing import HostIdentity
from scripts import macos_repository_maintenance as ctrl


class HostControlRelocationTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name).resolve()
        self.state = self.home / "state"
        self.state.mkdir(mode=0o700)
        self.root = self.home / "session"
        self.root.mkdir(mode=0o700)
        (self.root / "session.lock").touch(mode=0o600)
        self.source = self.home / "installed"
        self.source.mkdir()
        self.old = self.home / "old-release/host-control"
        self.old.mkdir(parents=True)
        self.git("init", "--quiet", "--initial-branch=agent-control")
        (self.old / "durable-result.json").write_text('{"status":"done"}\n')
        self.git("add", "durable-result.json")
        self.git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
                 "commit", "--quiet", "-m", "Preserve result history")
        self.git("remote", "add", "origin", "https://github.com/example-owner/local-agent.git")
        self.head = self.git("rev-parse", "HEAD")
        self.identity = self.state / "host-identity.json"
        identity = HostIdentity("fixture-mac", "b0e94f93-478f-4fc6-abed-159d3702362f")
        self.identity.write_text(json.dumps(identity.as_record()))
        self.identity.chmod(0o600)
        self.config = self.state / "supervisor-control.json"
        self.original = (json.dumps({
            "version": 1, "enabled": True, "host_id": identity.host_id,
            "identity_file": str(self.identity), "control_checkout": str(self.old),
            "repository": "example-owner/local-agent",
            "host_intake": {"enabled": True, "authorization": "github_writers"},
        }, indent=3) + "\n").encode()
        self.config.write_bytes(self.original)
        self.config.chmod(0o600)
        (self.state / "agentd.lock").touch(mode=0o600)
        for leaf in ("claims", "corrupt-claims", "result-spool"):
            (self.state / leaf).mkdir(mode=0o700)
        self.move = ctrl._control_move_plan(self.state, self.source)
        self.new = Path(self.move["new"])
        (self.root / "host-control-backup.json").write_bytes(self.original)
        self.record = {
            "state": "offline", "session_id": "session-" + "a" * 24,
            "source_sha": "a" * 40, "control_move": self.move,
            "host_control_sha256": hashlib.sha256(self.original).hexdigest(),
        }

    def git(self, *args, path: Path | None = None) -> str:
        return subprocess.check_output(
            ("git", "-C", str(path or self.old), *args), text=True,
            stderr=subprocess.PIPE, timeout=15,
        ).strip()

    def apply(self, *, rollback: bool = False) -> None:
        ctrl._move_control(self.root, self.record, self.source, self.state, rollback=rollback)

    def test_move_preserves_git_history_and_changes_only_checkout_selection(self) -> None:
        self.apply()
        self.assertFalse(self.old.exists())
        self.assertEqual(self.git("rev-parse", "HEAD", path=self.new), self.head)
        self.assertEqual(self.git("status", "--porcelain", path=self.new), "")
        expected = {**json.loads(self.original), "control_checkout": str(self.new)}
        self.assertEqual(json.loads(self.config.read_bytes()), expected)
        self.assertEqual((self.new / "durable-result.json").read_text(), '{"status":"done"}\n')
        self.assertIsNone(ctrl._control_move_plan(self.state, self.source))

    def test_relocation_and_rollback_are_idempotent(self) -> None:
        self.apply()
        self.apply()
        self.apply(rollback=True)
        self.apply(rollback=True)
        self.assertEqual(self.config.read_bytes(), self.original)
        self.assertEqual(self.git("rev-parse", "HEAD"), self.head)
        self.assertFalse(self.new.exists())

    def test_crash_between_rename_and_config_commit_can_be_rolled_back(self) -> None:
        with mock.patch.object(ctrl, "_atomic", side_effect=OSError("simulated interruption")):
            with self.assertRaisesRegex(OSError, "interruption"):
                self.apply()
        self.assertTrue(self.new.is_dir())
        self.assertEqual(self.config.read_bytes(), self.original)
        self.apply(rollback=True)
        self.assertTrue(self.old.is_dir())
        self.assertFalse(self.new.exists())

    def test_live_daemon_lock_blocks_move_before_effects(self) -> None:
        with (self.state / "agentd.lock").open("r+") as handle:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaisesRegex(RuntimeError, "daemon is active"):
                self.apply()
        self.assertTrue(self.old.is_dir())
        self.assertEqual(self.config.read_bytes(), self.original)

    def test_old_and_new_checkout_leases_both_block_relocation(self) -> None:
        for path in (self.old, self.new):
            with self.subTest(path=path):
                key = f"host-control-checkout:{path}"
                lease = acquire_execution_leases(self.state / "locks/repositories", (key,))
                try:
                    with self.assertRaises(ExecutionLeaseBusy):
                        self.apply()
                finally:
                    lease.close()
                self.assertTrue(self.old.is_dir())
                self.assertEqual(self.config.read_bytes(), self.original)

    def test_foreign_config_or_occupied_destination_remains_untouched(self) -> None:
        self.config.write_bytes(b'{"foreign":"preserve"}\n')
        with self.assertRaisesRegex(ctrl.MaintenanceBlocked, "outside maintenance"):
            self.apply()
        self.assertEqual(self.config.read_bytes(), b'{"foreign":"preserve"}\n')
        self.config.write_bytes(self.original)
        self.new.mkdir()
        (self.new / "owner.txt").write_text("preserve")
        with self.assertRaisesRegex(ctrl.MaintenanceBlocked, "ambiguous"):
            self.apply()
        self.assertEqual((self.new / "owner.txt").read_text(), "preserve")
        self.assertTrue(self.old.is_dir())

    def test_dirty_or_path_bound_git_checkout_is_not_moved(self) -> None:
        (self.old / "owner.txt").write_text("preserve")
        with self.assertRaisesRegex(ctrl.MaintenanceBlocked, "local work"):
            self.apply()
        (self.old / "owner.txt").unlink()
        self.git("config", "core.worktree", str(self.old))
        with self.assertRaisesRegex(ctrl.MaintenanceBlocked, "worktree configuration"):
            self.apply()
        self.assertTrue(self.old.is_dir())

    def test_symbolic_destination_or_corrupt_backup_is_rejected(self) -> None:
        self.new.symlink_to(self.source, target_is_directory=True)
        with self.assertRaisesRegex(ctrl.MaintenanceBlocked, "occupied"):
            ctrl._control_move_plan(self.state, self.source)
        self.new.unlink()
        (self.root / "host-control-backup.json").write_bytes(b"corrupt")
        with self.assertRaisesRegex(ctrl.MaintenanceBlocked, "backup changed"):
            self.apply()
        self.assertEqual(self.config.read_bytes(), self.original)

    def test_independent_watch_restores_directory_and_exact_config_after_crash(self) -> None:
        self.apply()
        original_registry = b'{"version":1,"repositories":[]}\n'
        (self.state / "repositories.json").write_bytes(original_registry)
        (self.root / "registry-backup.json").write_bytes(original_registry)
        (self.state / ctrl.FROZEN).write_bytes(ctrl._owned_freeze(self.record))
        with (
            mock.patch.object(ctrl, "_verify_session", return_value=(
                self.record, self.source, self.state, self.home / "service.plist", self.home,
            )),
            mock.patch.object(ctrl, "_generation", return_value="absent"),
            mock.patch.object(ctrl, "_resume") as resume,
            mock.patch.object(ctrl, "_retire") as retire,
        ):
            ctrl.watch(self.root)
        resume.assert_called_once()
        retire.assert_called_once()
        self.assertTrue(self.old.is_dir())
        self.assertEqual(self.config.read_bytes(), self.original)
        self.assertFalse((self.state / ctrl.FROZEN).exists())
        self.assertEqual(json.loads((self.root / ctrl.JOURNAL).read_text())["state"], "rolled_back")


if __name__ == "__main__":
    unittest.main()
