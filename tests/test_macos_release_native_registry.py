"""macOS B1c: native RepositoryContext routing in disposable queue fixtures."""
from __future__ import annotations

import datetime as dt
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest

from local_agent.supervisor import macos_release_admission as gate
from scripts import isolated_rollout_controller as rollout
from scripts import macos_release_fence_rehearsal as fence
from scripts.macos_release_quiescence_rehearsal import inspect_disposable_mac_quiescence


@unittest.skipUnless(sys.platform == "darwin", "Mac-only source acceptance")
class NativeRegistryMacEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="la-rollout-mac-real-routing-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "la-rollout-native-registry"
        self.root.mkdir(mode=0o700)
        (self.root / rollout.MARKER).write_text(rollout.MARKER_VALUE)
        self.state = fence.stage_disposable_opt_in(self.root)
        self.ids = tuple(f"project-{i}" for i in range(9))
        self.registry = self.state / "repositories.json"
        self.entries = []
        for idx, identifier in enumerate(self.ids):
            base = self.root / "real-workspaces" / identifier
            control, work, checkpoints = (base / name for name in
                                          ("control", "work", "checkpoints"))
            for path in (control, work, checkpoints, control / ".agent"):
                self.mkdir(path)
            host = idx == 3
            namespace = control / ".agent"
            local_state = self.state / "repositories" / identifier
            if host:
                namespace = namespace / "hosts" / "mac-fixture"
                local_state = local_state / "hosts" / "mac-fixture"
            for path in (namespace / "tasks", namespace / "results",
                         local_state / "claims", local_state / "result-spool"):
                self.mkdir(path)
            entry = {
                "id": identifier, "repository": f"example-owner/mock-{idx}",
                "control_dir": str(control), "work_dir": str(work),
                "checkpoints_dir": str(checkpoints), "enabled": True,
            }
            if host:
                entry.update(host_mailbox_enabled=True, host_id="mac-fixture")
            self.entries.append(entry)
        for name in ("requests", "claims", "results", "remote-approvals"):
            self.mkdir(self.state / "host-tasks" / name)
        for name in ("requests", "results", "launches"):
            self.mkdir(self.state / "conversation-operator" / name)
        self.record(self.state / "status.json", {
            "state": "idle", "self_revision": rollout.BASE_SHA,
            "pid": os.getpid(), "updated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        })
        self.save_registry()
        # Parent creation is subject to umask, even when leaves use 0700.
        # Normalize this disposable fixture before the strict read-only pass.
        for path in self.root.rglob("*"):
            if path.is_dir():
                path.chmod(0o700)
        fence.freeze_disposable_mac_admission(self.root)

    def mkdir(self, path):
        path.mkdir(parents=True, mode=0o700, exist_ok=True)
        node = path
        while self.root in node.parents:
            node.chmod(0o700)
            node = node.parent

    def record(self, path, data):
        path.write_text(json.dumps(data, sort_keys=True) + "\n")
        path.chmod(0o600)

    def save_registry(self):
        self.record(self.registry, {"version": 1, "repositories": self.entries})

    def inspect(self):
        return inspect_disposable_mac_quiescence(
            self.root, project_ids=self.ids, layout_mode="native_registry",
        )

    def test_actual_context_routing_for_nine_projects_and_host_namespace(self):
        receipt = self.inspect()
        self.assertTrue(receipt["quiescent_observation"])
        self.assertEqual(receipt["layout_mode"], "native_registry")
        self.assertEqual(receipt["project_count"], 9)
        self.assertEqual(len(receipt["registry_routing"]), 9)
        self.assertEqual(receipt["registry_routing"]["project-3"]["host_id"], "mac-fixture")
        self.assertTrue(receipt["registry_routing"]["project-3"]["host_mailbox"])
        self.assertFalse(receipt["registry_routing"]["project-2"]["host_mailbox"])
        self.assertEqual(len(receipt["registry_routing"]["project-3"]["config_digest"]), 64)
        self.assertFalse(receipt["source_switch_authorized"])
        self.assertFalse(receipt["real_queue_exclusion_proven"])
        self.assertTrue((self.state / gate.FROZEN).exists())

    def test_real_host_namespace_pending_blocks(self):
        path = (self.root / "real-workspaces/project-3/control"
                / ".agent/hosts/mac-fixture/tasks/pending.json")
        self.record(path, {"id": "pending"})
        receipt = self.inspect()
        self.assertEqual(receipt["projects"]["project-3"]["pending"], 1)
        self.assertFalse(receipt["quiescent_observation"])

    def test_nonhost_remote_pending_blocks(self):
        path = self.root / "real-workspaces/project-4/control/.agent/tasks/new.json"
        self.record(path, {"id": "new"})
        self.assertFalse(self.inspect()["quiescent_observation"])

    def test_host_claim_and_spool_under_native_routing_block(self):
        path = self.state / "repositories/project-3/hosts/mac-fixture/claims/unknown.json"
        self.record(path, {"status": "unknown"})
        self.assertEqual(self.inspect()["projects"]["project-3"]["claims"], 1)
        self.assertFalse(self.inspect()["quiescent_observation"])

    def test_reject_missing_registry_no_legacy_fallback(self):
        self.registry.unlink()
        with self.assertRaisesRegex(rollout.RolloutRejected, "missing private"):
            self.inspect()

    def test_reject_extra_or_disabled_registry_entry(self):
        self.entries.append({
            "id": "extra", "repository": "example-owner/unlisted",
            "enabled": False,
        })
        self.save_registry()
        with self.assertRaisesRegex(rollout.RolloutRejected, "incomplete native"):
            self.inspect()
        self.entries.pop()
        self.entries[1]["enabled"] = False
        self.save_registry()
        with self.assertRaisesRegex(rollout.RolloutRejected, "disabled"):
            self.inspect()

    def test_reject_duplicate_registry_keys(self):
        self.registry.write_text('{"version":1,"version":1,"repositories":[]}\n')
        self.registry.chmod(0o600)
        with self.assertRaisesRegex(rollout.RolloutRejected, "duplicate"):
            self.inspect()

    def test_reject_misrouted_host_state(self):
        path = self.state / "repositories/project-3/hosts/different"
        self.mkdir(path)
        with self.assertRaisesRegex(rollout.RolloutRejected, "host state routing"):
            self.inspect()

    def test_reject_wrong_host_namespace_or_missing_mailbox(self):
        directory = (self.root / "real-workspaces/project-3/control"
                     / ".agent/hosts/mac-fixture/results")
        directory.rmdir()
        with self.assertRaisesRegex(rollout.RolloutRejected, "missing release evidence"):
            self.inspect()

    def test_reject_external_or_redirected_checkout(self):
        self.entries[0]["control_dir"] = "/tmp/non-owned-checkout"
        self.save_registry()
        with self.assertRaisesRegex(rollout.RolloutRejected, "escapes Mac sandbox"):
            self.inspect()

    def test_reject_symlinked_checkout_parent(self):
        base = self.root / "real-workspaces/project-0"
        target = self.root / "moved-project"
        base.rename(target)
        base.symlink_to(target, target_is_directory=True)
        # repository_from_dict resolves the symlink. Our strict sandbox
        # boundary remains inside the test root, but the pre-resolved path
        # check must still reject the registry route after digest changes.
        with self.assertRaisesRegex(rollout.RolloutRejected, "symlinked release evidence"):
            self.inspect()

    def test_native_host_identity_file_must_be_private_and_not_redirected(self):
        identity = self.root / "real-workspaces/project-3/host-identity.json"
        self.record(identity, {"host_id": "mac-fixture"})
        self.entries[3]["host_identity_file"] = str(identity)
        self.save_registry()
        self.assertTrue(self.inspect()["quiescent_observation"])
        identity.chmod(0o644)
        with self.assertRaisesRegex(rollout.RolloutRejected, "ownership/mode"):
            self.inspect()
        identity.chmod(0o600)
        target = self.root / "other-identity.json"
        self.record(target, {"host_id": "mac-fixture"})
        identity.unlink()
        identity.symlink_to(target)
        with self.assertRaisesRegex(rollout.RolloutRejected, "symlinked release evidence"):
            self.inspect()

    def test_reject_untrusted_registry_permissions(self):
        self.registry.chmod(0o644)
        with self.assertRaisesRegex(rollout.RolloutRejected, "ownership/mode"):
            self.inspect()


if __name__ == "__main__":
    unittest.main()
