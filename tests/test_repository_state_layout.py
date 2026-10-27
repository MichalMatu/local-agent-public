"""Registration and admitted recovery preserve private repository ledgers."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from local_agent.repository.state_layout import (
    prepare_repository_state, recover_claims_with_quarantine_layout,
)
from local_agent.runtime.backlog_snapshot import observe_local_backlog


class QuarantineLayoutTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="la-state-layout-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.parent = self.root / "repositories" / "local-agent"
        self.claims = self.parent / "claims"
        self.quarantine = self.parent / "corrupt-claims"

    def test_registration_publishes_complete_private_layout_and_preserves_history(self) -> None:
        prepare_repository_state(self.root, "local-agent", create=True)
        for leaf in ("claims", "result-spool", "corrupt-claims"):
            path = self.parent / leaf
            self.assertEqual(path.stat().st_mode & 0o777, 0o700)
            (path / "retained.json").write_text("durable")
        prepare_repository_state(self.root, "local-agent", create=True)
        prepare_repository_state(self.root, "local-agent", create=False)
        for leaf in ("claims", "result-spool", "corrupt-claims"):
            self.assertEqual((self.parent / leaf / "retained.json").read_text(), "durable")

    def test_incomplete_existing_ledger_is_never_repaired(self) -> None:
        self.recover()
        with self.assertRaises(FileNotFoundError):
            prepare_repository_state(self.root, "local-agent", create=True)
        self.assertFalse(self.quarantine.exists())
        self.assertFalse((self.parent / "result-spool").exists())

    def test_linked_ledger_parent_is_refused_without_external_changes(self) -> None:
        external = self.root / "external"
        external.mkdir()
        (self.root / "repositories").symlink_to(external, target_is_directory=True)
        with self.assertRaises(OSError):
            prepare_repository_state(self.root, "local-agent", create=True)
        self.assertEqual(list(external.iterdir()), [])

    def test_failed_preparation_never_exposes_partial_project_ledger(self) -> None:
        with mock.patch("local_agent.repository.state_layout.os.rename",
                        side_effect=OSError("interrupted publication")):
            with self.assertRaisesRegex(OSError, "interrupted publication"):
                prepare_repository_state(self.root, "local-agent", create=True)
        self.assertFalse(self.parent.exists())
        prepare_repository_state(self.root, "local-agent", create=True)
        self.assertTrue(self.claims.is_dir())
        self.assertTrue(self.quarantine.is_dir())

    def test_registered_missing_ledger_is_not_initialized_on_retry(self) -> None:
        (self.root / "repositories").mkdir()
        with self.assertRaisesRegex(RuntimeError, "project ledger missing"):
            prepare_repository_state(self.root, "local-agent", create=False)
        self.assertFalse(self.parent.exists())

    def recover(self) -> None:
        self.claims.mkdir(parents=True, exist_ok=True)

    def test_missing_quarantine_is_created_privately_and_idempotently(self) -> None:
        self.assertFalse(self.quarantine.exists())
        recover_claims_with_quarantine_layout(self.recover, self.quarantine, self.root)
        self.assertTrue(self.quarantine.is_dir())
        self.assertEqual(self.quarantine.stat().st_mode & 0o777, 0o700)
        untouched = self.quarantine / "retained.json"
        untouched.write_text("durable", encoding="utf-8")
        recover_claims_with_quarantine_layout(self.recover, self.quarantine, self.root)
        self.assertEqual(untouched.read_text(encoding="utf-8"), "durable")

    def test_recovery_failure_keeps_safe_empty_quarantine(self) -> None:
        def fail() -> None:
            raise RuntimeError("recovery unavailable")
        with self.assertRaisesRegex(RuntimeError, "recovery unavailable"):
            recover_claims_with_quarantine_layout(fail, self.quarantine, self.root)
        self.assertTrue(self.quarantine.is_dir())
        self.assertFalse(self.claims.exists())

    def test_symlinked_quarantine_refused_without_following(self) -> None:
        self.recover()
        target = self.root / "external"
        target.mkdir()
        (target / "untouched.json").write_text("do-not-delete", encoding="utf-8")
        self.quarantine.symlink_to(target, target_is_directory=True)
        with self.assertRaises(OSError):
            recover_claims_with_quarantine_layout(self.recover, self.quarantine, self.root)
        self.assertTrue(self.quarantine.is_symlink())
        self.assertEqual((target / "untouched.json").read_text(), "do-not-delete")

    def test_untrusted_existing_quarantine_refused(self) -> None:
        self.recover()
        self.quarantine.mkdir(mode=0o700)
        self.quarantine.chmod(0o777)
        with self.assertRaises(ValueError):
            recover_claims_with_quarantine_layout(self.recover, self.quarantine, self.root)
        self.assertEqual(self.quarantine.stat().st_mode & 0o777, 0o777)

    def test_untrusted_parent_refused_without_creation(self) -> None:
        self.recover()
        self.parent.chmod(0o777)
        with self.assertRaises(ValueError):
            recover_claims_with_quarantine_layout(self.recover, self.quarantine, self.root)
        self.assertFalse(self.quarantine.exists())

    def test_parent_symlink_refused_without_creation(self) -> None:
        target = self.root / "external"
        target.mkdir()
        (target / "claims").mkdir()
        self.parent.parent.mkdir()
        self.parent.symlink_to(target, target_is_directory=True)
        with self.assertRaises(OSError):
            recover_claims_with_quarantine_layout(lambda: None, self.quarantine, self.root)
        self.assertFalse((target / "corrupt-claims").exists())

    def test_readonly_census_only_recovers_after_worker_initialization(self) -> None:
        state = self.root / "state"
        parent = state / "repositories" / "local-agent"
        (parent / "claims").mkdir(parents=True)
        (parent / "result-spool").mkdir()
        registry = self.root / "registry.json"
        registry.write_text(json.dumps({
            "version": 1, "repositories": [{"id": "local-agent"}],
        }), encoding="utf-8")
        registry.chmod(0o600)
        before = observe_local_backlog(state_root=state, registry_file=registry)
        self.assertEqual(before["status"], "unverified")
        self.assertIn("corrupt_claims_unavailable", before["warnings"])
        self.assertFalse((parent / "corrupt-claims").exists())
        recover_claims_with_quarantine_layout(lambda: None, parent / "corrupt-claims", state)
        after = observe_local_backlog(state_root=state, registry_file=registry)
        self.assertEqual(after["status"], "observed")
        self.assertEqual(after["totals"], {
            "claims": 0, "result-spool": 0, "corrupt-claims": 0,
        })
        self.assertFalse(after["mutated"])

    def test_repositories_ancestor_symlink_cannot_escape_state(self) -> None:
        state = self.root / "state"
        state.mkdir()
        outside = self.root / "external"
        (outside / "local-agent").mkdir(parents=True)
        (state / "repositories").symlink_to(outside, target_is_directory=True)
        target = state / "repositories/local-agent/corrupt-claims"
        with self.assertRaises(OSError):
            recover_claims_with_quarantine_layout(
                lambda: None, target, state,
            )
        self.assertFalse((outside / "local-agent/corrupt-claims").exists())

    def test_host_namespace_ancestor_symlink_cannot_escape_state(self) -> None:
        state = self.root / "state"
        base = state / "repositories/local-agent"
        base.mkdir(parents=True)
        outside = self.root / "outside-hosts"
        (outside / "worker-a").mkdir(parents=True)
        (base / "hosts").symlink_to(outside, target_is_directory=True)
        target = base / "hosts/worker-a/corrupt-claims"
        with self.assertRaises(OSError):
            recover_claims_with_quarantine_layout(
                lambda: None, target, state,
            )
        self.assertFalse((outside / "worker-a/corrupt-claims").exists())

    def test_valid_host_namespace_creates_quarantine_inside_state(self) -> None:
        state = self.root / "state"
        target = state / "repositories/local-agent/hosts/worker-a/corrupt-claims"
        recover_claims_with_quarantine_layout(lambda: None, target, state)
        self.assertTrue(target.is_dir())
        self.assertEqual(target.stat().st_mode & 0o777, 0o700)

    def test_state_root_symlink_rejected(self) -> None:
        outside = self.root / "outside-root"
        outside.mkdir()
        state = self.root / "state"
        state.symlink_to(outside, target_is_directory=True)
        with self.assertRaises(OSError):
            recover_claims_with_quarantine_layout(
                lambda: None, state / "repositories/local-agent/corrupt-claims", state,
            )
        self.assertFalse((outside / "repositories").exists())

    def test_escape_path_rejected_before_any_directory_creation(self) -> None:
        target = self.root / "other" / "repositories" / "local-agent" / "corrupt-claims"
        with self.assertRaisesRegex(ValueError, "invalid quarantine directory identity"):
            recover_claims_with_quarantine_layout(lambda: None, target, self.root)
        self.assertFalse((self.root / "other").exists())

    def test_wrong_directory_name_refused(self) -> None:
        self.recover()
        with self.assertRaisesRegex(ValueError, "identity"):
            recover_claims_with_quarantine_layout(self.recover, self.parent / "other", self.root)
        self.assertFalse((self.parent / "other").exists())


if __name__ == "__main__":
    unittest.main()
