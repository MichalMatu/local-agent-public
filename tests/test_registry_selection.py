"""Offline registry-pool transaction is byte-reversible and fail-closed."""
from __future__ import annotations

import fcntl
import hashlib
import json
import shutil
from pathlib import Path
import tempfile
import unittest

from local_agent.repository import registry_selection as selection


class OfflinePoolSelectionTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="local-agent-pool-")
        self.addCleanup(temp.cleanup)
        self.home = Path(temp.name)
        self.registry = self.home / "Library/Application Support/local-agent/repositories.json"
        self.registry.parent.mkdir(parents=True)
        self.state = self.registry.parent
        (self.state / "agentd.lock").write_text("offline")
        for leaf in ("claims", "corrupt-claims", "result-spool"):
            (self.state / leaf).mkdir()
        self.work = self.home / "important owner data"
        self.work.mkdir()
        (self.work / "dirty.txt").write_text("DO NOT TOUCH")
        self.control = self.home / "agent-control"
        self.control.mkdir()
        (self.control / "history").write_text("IMMUTABLE")
        self.checkpoints = self.home / "checkpoints"
        self.checkpoints.mkdir()
        (self.checkpoints / "retain").write_text("checkpoint")
        self.backup_dir = self.home / "rollback"
        self.backup_dir.mkdir(mode=0o700)
        self.backup = self.backup_dir / "registry-original.json"
        self._configure(9)

    def _configure(self, count):
        rows = []
        for i in range(count):
            ident = f"repo-{i}"
            rows.append({
                "id": ident, "repository": f"owner/{ident}", "enabled": True,
                "work_dir": str(self.work if i == 0 else self.home / ident / "work"),
                "control_dir": str(self.control if i == 0 else self.home / ident / "control"),
                "checkpoints_dir": str(self.checkpoints if i == 0 else
                                       self.home / ident / "checkpoints"),
                "work_mode": "attached", "custom": {"keep": i},
            })
            ledger = self.state / "repositories" / ident
            ledger.mkdir(parents=True, exist_ok=True)
            for leaf in ("claims", "result-spool"):
                (ledger / leaf).mkdir(exist_ok=True)
        self.registry.write_text(json.dumps({"version": 1, "repositories": rows},
                                            indent=1) + "\n")

    def _plan(self, ids):
        return selection.plan_repository_selection(ids, registry_path=self.registry,
                                                   home=self.home)

    def _apply(self, ids, digest=None):
        return selection.apply_repository_selection(
            ids, expected_sha256=digest or self._plan(ids)["expected_sha256"],
            backup_path=self.backup, registry_path=self.registry,
            state_dir=self.state, home=self.home,
        )

    def _restore(self, applied, original):
        return selection.restore_repository_selection(
            expected_applied_sha256=applied,
            original_sha256=original, backup_path=self.backup,
            registry_path=self.registry, state_dir=self.state,
            home=self.home,
        )

    def test_selection_no_count_cap_and_byte_exact_restore(self):
        for count in (0, 3, 4, 9):
            with self.subTest(enabled_count=count):
                original = self.registry.read_bytes()
                ids = [f"repo-{i}" for i in range(count)]
                plan = self._plan(ids)
                self.assertEqual(self.registry.read_bytes(), original)
                result = self._apply(ids)
                self.assertEqual(result["status"], "applied")
                self.assertEqual(self.backup.read_bytes(), original)
                self.assertEqual(
                    [x["id"] for x in json.loads(self.registry.read_text())["repositories"]
                     if x["enabled"]], ids,
                )
                self.assertEqual(self._restore(result["applied_sha256"],
                                               plan["expected_sha256"])["status"],
                                 "restored")
                self.assertEqual(self.registry.read_bytes(), original)
                self.assertEqual((self.work / "dirty.txt").read_text(), "DO NOT TOUCH")
                self.assertEqual((self.control / "history").read_text(), "IMMUTABLE")
                self.assertEqual((self.checkpoints / "retain").read_text(), "checkpoint")

    def test_host_only_empty_registry_needs_no_project_ledger_directory(self):
        self._configure(0)
        shutil.rmtree(self.state / "repositories")
        original = self.registry.read_bytes()
        outcome = self._apply([])
        self.assertEqual(outcome["enabled"], [])
        self.assertEqual(self._restore(outcome["applied_sha256"],
                                       outcome["previous_sha256"])["status"], "restored")
        self.assertEqual(self.registry.read_bytes(), original)

    def test_more_than_64_registrations_and_no_fixed_count(self):
        self._configure(70)
        original = self.registry.read_bytes()
        ids = [f"repo-{i}" for i in range(0, 70, 2)]
        result = self._apply(ids)
        self.assertEqual(len(result["enabled"]), 35)
        self.assertEqual(self.backup.read_bytes(), original)
        self.assertEqual(self._restore(result["applied_sha256"],
                                       result["previous_sha256"])["status"], "restored")
        self.assertEqual(self.registry.read_bytes(), original)

    def test_active_daemon_lock_blocks_without_backup(self):
        original = self.registry.read_bytes()
        with (self.state / "agentd.lock").open("r+") as f:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaisesRegex(RuntimeError, "daemon is active"):
                self._apply(["repo-0"])
        self.assertEqual(self.registry.read_bytes(), original)
        self.assertFalse(self.backup.exists())

    def test_unfinished_ledger_in_disabled_lane_blocks_selection_and_restore(self):
        original = self.registry.read_bytes()
        pending = self.state / "repositories/repo-8/result-spool/pending.json"
        pending.write_text("{}")
        with self.assertRaisesRegex(RuntimeError, "unresolved project ledger"):
            self._apply(["repo-0"])
        self.assertEqual(self.registry.read_bytes(), original)
        self.assertFalse(self.backup.exists())
        pending.unlink()
        result = self._apply(["repo-0"])
        pending.write_text("{}")
        with self.assertRaisesRegex(RuntimeError, "unresolved project ledger"):
            self._restore(result["applied_sha256"], result["previous_sha256"])
        pending.unlink()
        self.assertEqual(self._restore(result["applied_sha256"],
                                       result["previous_sha256"])["status"], "restored")

    def test_digest_stale_and_unknown_selected_fail_before_any_backup(self):
        original = self.registry.read_bytes()
        digest = self._plan(["repo-0"])["expected_sha256"]
        self.registry.write_bytes(original + b" ")
        changed = self.registry.read_bytes()
        with self.assertRaisesRegex(RuntimeError, "changed since selection"):
            self._apply(["repo-0"], digest)
        for bad in (["unregistered"], ["repo-0", "REPO-0"], "repo-0"):
            with self.assertRaises((ValueError, TypeError)):
                self._apply(bad)
        self.assertEqual(self.registry.read_bytes(), changed)
        self.assertFalse(self.backup.exists())

    def test_tampered_backup_blocks_reapply_and_restore(self):
        result = self._apply(["repo-0"])
        original_sha = result["previous_sha256"]
        self.backup.write_text("tampered")
        with self.assertRaisesRegex(RuntimeError, "backup does not match"):
            self._restore(result["applied_sha256"], original_sha)
        self.assertEqual(hashlib.sha256(self.registry.read_bytes()).hexdigest(),
                         result["applied_sha256"])

    def test_restore_rejects_third_party_registry_change(self):
        result = self._apply(["repo-0"])
        self.registry.write_bytes(self.registry.read_bytes() + b" ")
        third_party = self.registry.read_bytes()
        with self.assertRaisesRegex(RuntimeError, "unrelated changes"):
            self._restore(result["applied_sha256"], result["previous_sha256"])
        self.assertEqual(self.registry.read_bytes(), third_party)

    def test_host_terminal_claim_history_is_preserved(self):
        claims = self.state / "host-tasks/claims"
        results = self.state / "host-tasks/results"
        claims.mkdir(parents=True)
        results.mkdir()
        row = {"id": "finished", "status": "claimed", "task_digest": "a" * 64,
               "target_host_id": "example-mac",
               "target_installation_id": "installation"}
        (claims / "finished.json").write_text(json.dumps(row))
        (results / "finished.json").write_text(json.dumps({**row, "status": "done"}))
        outcome = self._apply(["repo-0"])
        self.assertEqual(outcome["status"], "applied")
        self.assertTrue((claims / "finished.json").exists())
        self.assertTrue((results / "finished.json").exists())
        self.assertEqual(self._restore(outcome["applied_sha256"],
                                       outcome["previous_sha256"])["status"], "restored")

    def test_missing_backup_and_symlink_are_refused_on_restore(self):
        result = self._apply(["repo-0"])
        self.backup.unlink()
        with self.assertRaisesRegex(RuntimeError, "backup missing"):
            self._restore(result["applied_sha256"], result["previous_sha256"])
        foreign = self.home / "foreign"
        foreign.write_text("DO NOT CHANGE")
        self.backup.symlink_to(foreign)
        with self.assertRaisesRegex(RuntimeError, "backup missing"):
            self._restore(result["applied_sha256"], result["previous_sha256"])
        self.assertEqual(foreign.read_text(), "DO NOT CHANGE")


if __name__ == "__main__":
    unittest.main()
