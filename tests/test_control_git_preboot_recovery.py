"""Real shallow Git regression for a redundant lock left by an earlier boot."""
from __future__ import annotations

from contextlib import nullcontext
import os
from pathlib import Path
import subprocess
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest import mock

from local_agent.foundation import git_lock_recovery as recovery
from local_agent.foundation import storage


class PrebootShallowRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.producer = self.root / "producer"
        self.control = self.root / "control"
        self.producer.mkdir()
        self.git("init", "-q", "-b", "agent-control", cwd=self.producer)
        self.git("config", "user.name", "Fixture", cwd=self.producer)
        self.git("config", "user.email", "fixture@local.invalid", cwd=self.producer)
        (self.producer / "owner.txt").write_text("preserve\n")
        self.git("add", ".", cwd=self.producer)
        self.git("commit", "-qm", "base", cwd=self.producer)
        self.git("clone", "-q", "--depth=1", self.producer.as_uri(), str(self.control))
        self.shallow = self.control / ".git/shallow"
        self.lock = self.control / ".git/shallow.lock"
        self.lock.write_bytes(self.shallow.read_bytes())
        self.boot = {"exit_code": 0, "output":
                     "{ sec = " + str(int(time.time()) + 60)
                     + ", usec = 0 } Tue Oct 27 06:57:51 2026\n"}
        self.holders = {"exit_code": 1, "output": ""}
        self.on_holder_check = lambda: None
        self.core = SimpleNamespace(
            CONTROL=self.control, CONTROL_BRANCH="agent-control",
            CONTROL_GIT_LOCK=nullcontext(), process=self.process, log=mock.Mock(),
        )

    def git(self, *args, cwd=None, check=True):
        return subprocess.run(["git", *args], cwd=cwd or self.root,
                              capture_output=True, text=True, check=check, timeout=15)

    def process(self, args, cwd, **kwargs):
        if args[0] == "/usr/sbin/sysctl":
            return self.boot
        if args[0] == "/usr/sbin/lsof":
            self.on_holder_check()
            return self.holders
        result = subprocess.run(args, cwd=cwd, capture_output=True, text=True,
                                timeout=kwargs.get("timeout", 15), check=False)
        return {"exit_code": result.returncode, "output": result.stdout + result.stderr}

    def test_real_git_failure_recovers_and_next_sync_succeeds(self):
        before = self.shallow.read_bytes()
        self.assertNotEqual(self.git("fetch", "--depth=1", "origin",
                                     cwd=self.control, check=False).returncode, 0)
        storage.sync_control(self.core, tasks_dir=".agent/hosts/fixture-host/tasks")
        self.assertFalse(self.lock.exists())
        backups = list((self.control / ".git").glob("local-agent-preboot-shallow-*.lock"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_bytes(), before)
        self.assertEqual(backups[0].stat().st_mode & 0o777, 0o600)
        storage.sync_control(self.core, tasks_dir=".agent/hosts/fixture-host/tasks")
        self.assertEqual(len(list((self.control / ".git").glob(
            "local-agent-preboot-shallow-*.lock"))), 1)

    def test_recovery_does_not_modify_index_tasks_or_untracked_owner_files(self):
        task = self.control / ".agent/tasks/owner.json"
        task.parent.mkdir(parents=True)
        task.write_text('{"id":"owner"}\n')
        self.git("add", str(task.relative_to(self.control)), cwd=self.control)
        extra = self.control / "valuable-untracked.txt"
        extra.write_text("valuable\n")
        before = self.git("status", "--porcelain=v1", cwd=self.control).stdout
        head = self.git("rev-parse", "HEAD", cwd=self.control).stdout
        recovery.recover_preboot_shallow_lock(self.core)
        self.assertEqual(self.git("status", "--porcelain=v1", cwd=self.control).stdout, before)
        self.assertEqual(self.git("rev-parse", "HEAD", cwd=self.control).stdout, head)
        self.assertEqual(task.read_text(), '{"id":"owner"}\n')
        self.assertEqual(extra.read_text(), "valuable\n")

    def test_current_boot_lock_is_preserved(self):
        self.boot["output"] = "{ sec = 1, usec = 0 }\n"
        recovery.recover_preboot_shallow_lock(self.core)
        self.assertTrue(self.lock.exists())

    def test_unavailable_boot_or_holder_evidence_preserves_lock(self):
        for boot in ({"exit_code": 1, "output": ""},
                     {"exit_code": 0, "output": "unparseable"}):
            self.boot = boot
            recovery.recover_preboot_shallow_lock(self.core)
            self.assertTrue(self.lock.exists())
        self.boot = {"exit_code": 0, "output": "{ sec = 9999999999, usec = 0 }"}
        for holders in ({"exit_code": 0, "output": "123\n"},
                        {"exit_code": 1, "output": "cannot inspect"},
                        {"exit_code": 1, "output": "", "timed_out": True}):
            self.holders = holders
            recovery.recover_preboot_shallow_lock(self.core)
            self.assertTrue(self.lock.exists())

    def test_different_shallow_contents_and_other_locks_are_preserved(self):
        self.lock.write_text("a" * 40 + "\n")
        index_lock = self.control / ".git/index.lock"
        index_lock.write_text("do not remove")
        recovery.recover_preboot_shallow_lock(self.core)
        self.assertTrue(self.lock.exists())
        self.assertEqual(index_lock.read_text(), "do not remove")

    def test_symlink_and_linked_metadata_fail_closed(self):
        self.lock.unlink()
        self.lock.symlink_to(self.shallow)
        with self.assertRaises(OSError):
            recovery.recover_preboot_shallow_lock(self.core)
        self.lock.unlink()
        os.link(self.shallow, self.lock)
        with self.assertRaisesRegex(RuntimeError, "unsafe"):
            recovery.recover_preboot_shallow_lock(self.core)
        self.assertTrue(self.lock.exists())

    def test_replacement_during_inspection_is_never_removed(self):
        def replace():
            self.lock.unlink()
            self.lock.write_text("replacement\n")
        self.on_holder_check = replace
        with self.assertRaisesRegex(RuntimeError, "changed before recovery"):
            recovery.recover_preboot_shallow_lock(self.core)
        self.assertEqual(self.lock.read_text(), "replacement\n")

    def test_failed_durable_backup_never_unlinks_lock(self):
        with mock.patch.object(recovery.os, "fsync", side_effect=OSError("disk failure")):
            with self.assertRaises(OSError):
                recovery.recover_preboot_shallow_lock(self.core)
        self.assertEqual(self.lock.read_bytes(), self.shallow.read_bytes())

    def test_access_time_update_is_not_mistaken_for_a_mutation(self):
        before = self.lock.stat()
        fields = {name: getattr(before, name) for name in (
            "st_dev", "st_ino", "st_mode", "st_uid", "st_gid", "st_nlink",
            "st_size", "st_mtime_ns", "st_ctime_ns",
        )}
        after = SimpleNamespace(**fields, st_atime_ns=before.st_atime_ns + 1)
        with mock.patch.object(recovery.os, "fstat", side_effect=[before, after]):
            _, raw = recovery._read(self.lock)
        self.assertEqual(raw, self.shallow.read_bytes())
        after.st_mtime_ns += 1
        with mock.patch.object(recovery.os, "fstat", side_effect=[before, after]):
            with self.assertRaisesRegex(RuntimeError, "changed while reading"):
                recovery._read(self.lock)
