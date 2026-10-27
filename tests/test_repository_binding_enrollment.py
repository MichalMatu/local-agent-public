"""Offline owner-selected binding catalog regressions; no production service."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from local_agent.repository import binding, binding_enrollment


class PrivateBindingEnrollmentTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory(prefix="la-private-bind-")
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.source = self.root / "source.json"
        self.source.write_text(json.dumps({
            "version": 1, "agents": [{
                "id": "existing", "repository": "Owner/existing",
                "agent_binding": "11111111-1111-4111-8111-111111111111",
                "execution_enabled": True,
            }],
        }), encoding="utf-8")
        self.private = self.root / "installation.json"
        self.source_before = self.source.read_bytes()
        self.patches = [
            mock.patch.object(binding, "DEFAULT_CATALOG_PATH", self.source),
            mock.patch.object(binding, "local_binding_catalog_path", return_value=self.private),
            mock.patch.object(binding_enrollment, "local_binding_catalog_path", return_value=self.private),
        ]
        for patcher in self.patches:
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_create_once_reuse_and_preserve_bundled_source(self) -> None:
        created = binding_enrollment.ensure_local_repository_binding("new", "Owner/new")
        self.assertEqual(binding.canonical_agent_binding(created), created)
        self.assertEqual(
            binding_enrollment.ensure_local_repository_binding("new", "Owner/new"),
            created,
        )
        self.assertEqual(self.source.read_bytes(), self.source_before)
        local = json.loads(self.private.read_text(encoding="utf-8"))
        self.assertEqual(len(local["agents"]), 1)
        self.assertEqual(local["agents"][0]["planner_scope"], "repository")
        self.assertEqual(
            binding.catalog_record_for_repository("new", "Owner/new").agent_binding,
            created,
        )

    def test_reuse_preexisting_bundle_without_writing_local_file(self) -> None:
        self.assertEqual(
            binding_enrollment.ensure_local_repository_binding("existing", "Owner/existing"),
            "11111111-1111-4111-8111-111111111111",
        )
        self.assertFalse(self.private.exists())

    def test_conflict_refuses_without_changes(self) -> None:
        with self.assertRaisesRegex(ValueError, "identity conflicts"):
            binding_enrollment.ensure_local_repository_binding("existing", "Other/existing")
        with self.assertRaisesRegex(ValueError, "identity conflicts"):
            binding_enrollment.ensure_local_repository_binding("different", "owner/EXISTING")
        with self.assertRaisesRegex(ValueError, "invalid repository"):
            binding_enrollment.ensure_local_repository_binding("bad", "not-an-owner-repo")
        self.assertFalse(self.private.exists())
        self.assertEqual(self.source.read_bytes(), self.source_before)

    def test_disabled_catalog_identity_cannot_be_reenabled_implicitly(self) -> None:
        source = json.loads(self.source.read_text())
        source["agents"][0]["execution_enabled"] = False
        self.source.write_text(json.dumps(source))
        with self.assertRaisesRegex(ValueError, "disabled"):
            binding_enrollment.ensure_local_repository_binding("existing", "Owner/existing")
        self.assertFalse(self.private.exists())

    def test_symlink_and_invalid_catalog_fail_closed(self) -> None:
        outside = self.root / "outside"
        outside.write_text("keep", encoding="utf-8")
        self.private.symlink_to(outside)
        with self.assertRaisesRegex(ValueError, "symlink"):
            binding_enrollment.ensure_local_repository_binding("new", "Owner/new")
        self.assertEqual(outside.read_text(), "keep")
        self.private.unlink()
        self.private.write_text('{"version":1,"agents":"invalid"}')
        with self.assertRaisesRegex(ValueError, "agents must be a list"):
            binding_enrollment.ensure_local_repository_binding("new", "Owner/new")
        self.assertEqual(self.source.read_bytes(), self.source_before)

    def test_duplicate_between_source_and_private_rejected(self) -> None:
        self.private.write_text(json.dumps({
            "version": 1, "agents": [{
                "id": "existing", "repository": "Owner/existing",
                "agent_binding": "11111111-1111-4111-8111-111111111111",
            }],
        }))
        with self.assertRaisesRegex(ValueError, "duplicate agent binding id"):
            binding.load_binding_catalog()

    def test_new_binding_validates_against_matching_private_control_identity(self) -> None:
        created = binding_enrollment.ensure_local_repository_binding("new", "Owner/new")
        control = self.root / "control"
        (control / ".agent").mkdir(parents=True)
        identity = control / ".agent" / "binding.json"
        payload = {
            "version": 1, "repository_id": "new",
            "repository": "Owner/new", "agent_binding": created,
        }
        identity.write_text(json.dumps(payload))
        self.assertEqual(binding.validate_repository_control_binding(
            repository_id="new", repository="Owner/new",
            expected_agent_binding=created, control_dir=control,
        ), created)
        payload["agent_binding"] = "22222222-2222-4222-8222-222222222222"
        identity.write_text(json.dumps(payload))
        with self.assertRaisesRegex(ValueError, "UUID mismatch"):
            binding.validate_repository_control_binding(
                repository_id="new", repository="Owner/new",
                expected_agent_binding=created, control_dir=control,
            )


if __name__ == "__main__":
    unittest.main()
