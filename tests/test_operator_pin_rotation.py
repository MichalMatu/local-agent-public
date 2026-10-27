"""Read-only B2 pin-rollover inspection in disposable local Git fixtures."""
from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from local_agent.operator import pin_rotation

PI_ID = "7d114aff-9d1a-4df1-849b-ae4a99b89db0"
MAC_ID = "e6610a5b-cb49-4027-83f2-2dcfdc4a66be"
PI_DIGEST = "b" * 64
MAC_DIGEST = "c" * 64
BINDING = "b0e94f93-478f-4fc6-abed-159d3702362f"


class PinRotationPreflightTests(unittest.TestCase):
    def setUp(self) -> None:
        root = tempfile.TemporaryDirectory()
        self.addCleanup(root.cleanup)
        self.root = Path(root.name)
        self.identity_file = self.root / "identity.json"
        self.identity_file.write_text(json.dumps({
            "version": 1, "host_id": "pi-zero2w", "installation_id": PI_ID,
        }) + "\n", encoding="utf-8")
        self.identity_file.chmod(0o600)
        self.pin = self.root / "operator-control.sha"
        self.repo = self.root / "operator"
        self.repo.mkdir()
        self.git("init", "-q")
        self.git("config", "user.name", "Offline Fixture")
        self.git("config", "user.email", "offline-fixture@example.invalid")
        self.git("checkout", "-q", "-b", "operator-control")
        self.hosts = {"version": 1, "hosts": [self.pi_enrollment()]}
        self.grants = {"version": 1, "grants": [self.pi_grant()]}
        self.write_manifests()
        self.old = self.commit("old")
        self.pin.write_text(self.old + "\n", encoding="ascii")
        self.pin.chmod(0o600)
        self.hosts["hosts"].append(self.mac_enrollment())
        self.grants["grants"].append(self.mac_grant())
        self.write_manifests()
        self.new = self.commit("candidate")

    def git(self, *argv: str) -> str:
        result = subprocess.run(
            ["git", "-C", str(self.repo), *argv], check=True, capture_output=True, text=True,
        )
        return result.stdout.strip()

    def commit(self, message: str) -> str:
        self.git("add", ".agent/operator/hosts.json", ".agent/operator/host-commands.json",
                 ".agent/operator/state.json")
        self.git("commit", "-qm", message, "--allow-empty")
        return self.git("rev-parse", "HEAD")

    @staticmethod
    def pi_enrollment():
        return {
            "version": 1, "host_id": "pi-zero2w",
            "installation_id": PI_ID, "enabled": True,
            "repositories": [{"repository_id": "local-agent", "agent_binding": BINDING}],
        }

    @staticmethod
    def mac_enrollment():
        return {
            "version": 1, "host_id": "example-mac",
            "installation_id": MAC_ID, "enabled": True, "repositories": [],
        }

    @staticmethod
    def pi_grant():
        return {
            "host_id": "pi-zero2w", "installation_id": PI_ID,
            "task_id": "pi-pilot", "task_digest": PI_DIGEST,
        }

    @staticmethod
    def mac_grant():
        return {
            "host_id": "example-mac", "installation_id": MAC_ID,
            "task_id": "mac-pilot", "task_digest": MAC_DIGEST,
        }

    def write_manifests(self):
        folder = self.repo / ".agent/operator"
        folder.mkdir(parents=True, exist_ok=True)
        for name, payload in (
            ("hosts.json", self.hosts),
            ("host-commands.json", self.grants),
        ):
            (folder / name).write_text(json.dumps(payload) + "\n", encoding="utf-8")
        state = folder / "state.json"
        if not state.exists():
            state.write_text('{"version":1,"desired_state":"enabled","request_id":"offline"}\n',
                             encoding="utf-8")

    def inspect(self, sha=None):
        with mock.patch.object(pin_rotation, "_require_trusted_host_registry_origin"), \
             mock.patch.object(pin_rotation, "_remote_ref", return_value=self.old):
            return pin_rotation.inspect_operator_pin_rotation(
                identity_file=self.identity_file,
                operator_checkout=self.repo,
                candidate_sha=self.new if sha is None else sha,
            )

    def test_additive_candidate_does_not_change_any_live_authority(self):
        before_pin = self.pin.read_bytes()
        before_head = self.git("rev-parse", "HEAD")
        result = self.inspect()
        self.assertEqual(result["decision"], "COMPATIBLE_NOT_AUTHORIZED")
        self.assertEqual((result["preserved_enrollments"], result["preserved_grants"]), (1, 1))
        self.assertEqual((result["new_enrollments"], result["new_grants"]), (1, 1))
        self.assertFalse(result["local_pin_updated"])
        self.assertFalse(result["operator_ref_updated"])
        self.assertEqual(self.pin.read_bytes(), before_pin)
        self.assertEqual(self.git("rev-parse", "HEAD"), before_head)

    def test_candidate_must_be_complete_direct_child_of_trusted_sha(self):
        for sha in ("f" * 39, "F" * 40, self.old):
            with self.subTest(sha=sha), self.assertRaises(ValueError):
                self.inspect(sha)
        self.git("commit", "-qm", "another", "--allow-empty")
        grandson = self.git("rev-parse", "HEAD")
        with self.assertRaisesRegex(ValueError, "direct child"):
            self.inspect(grandson)

    def test_changed_ref_missing_pin_and_untrusted_origin_fail_closed(self):
        with mock.patch.object(pin_rotation, "_require_trusted_host_registry_origin"), \
             mock.patch.object(pin_rotation, "_remote_ref", return_value="f" * 40), \
             self.assertRaisesRegex(ValueError, "locally trusted"):
            pin_rotation.inspect_operator_pin_rotation(
                identity_file=self.identity_file, operator_checkout=self.repo,
                candidate_sha=self.new,
            )
        with mock.patch.object(pin_rotation, "_require_trusted_host_registry_origin",
                               side_effect=ValueError("untrusted")), \
             self.assertRaisesRegex(ValueError, "untrusted"):
            pin_rotation.inspect_operator_pin_rotation(
                identity_file=self.identity_file, operator_checkout=self.repo,
                candidate_sha=self.new,
            )
        self.pin.unlink()
        with self.assertRaises(OSError):
            self.inspect()

    def test_existing_grant_removed_is_not_compatible(self):
        self.grants["grants"] = [self.mac_grant()]
        self.write_manifests()
        bad = self.commit("remove old approval")
        with self.assertRaisesRegex(ValueError, "direct child"):
            self.inspect(bad)
        self.git("reset", "--hard", self.old)
        self.write_manifests()
        bad = self.commit("remove old approval directly")
        with self.assertRaisesRegex(ValueError, "removes or modifies"):
            self.inspect(bad)

    def test_existing_project_enrollment_change_refused(self):
        self.git("reset", "--hard", self.old)
        self.hosts["hosts"][0]["repositories"] = []
        self.write_manifests()
        bad = self.commit("modify Pi binding")
        with self.assertRaisesRegex(ValueError, "removes or modifies"):
            self.inspect(bad)

    def test_new_host_cannot_claim_existing_projects(self):
        self.git("reset", "--hard", self.old)
        self.hosts["hosts"][1]["repositories"] = [
            {"repository_id": "project-x", "agent_binding": "00000000-0000-4000-8000-000000000002"},
        ]
        self.write_manifests()
        bad = self.commit("new host with project")
        with self.assertRaisesRegex(ValueError, "project-free"):
            self.inspect(bad)

    def test_extra_operator_state_change_is_refused(self):
        state = self.repo / ".agent/operator/state.json"
        state.write_text('{"version":1,"desired_state":"disabled","request_id":"other"}\n',
                         encoding="utf-8")
        bad = self.commit("change state")
        with self.assertRaisesRegex(ValueError, "direct child"):
            self.inspect(bad)
        self.git("reset", "--hard", self.old)
        state.write_text('{"version":1,"desired_state":"disabled","request_id":"other"}\n',
                         encoding="utf-8")
        self.write_manifests()
        bad = self.commit("change state directly")
        with self.assertRaisesRegex(ValueError, "unrelated operator"):
            self.inspect(bad)

    def test_unauthorized_extra_grant_to_existing_host_refused(self):
        self.git("reset", "--hard", self.old)
        self.grants["grants"].append({
            "host_id": "pi-zero2w", "installation_id": PI_ID,
            "task_id": "unapproved", "task_digest": "d" * 64,
        })
        self.write_manifests()
        bad = self.commit("extra existing-host grant")
        with self.assertRaisesRegex(ValueError, "newly enrolled"):
            self.inspect(bad)

    def test_symlink_or_world_readable_pin_refused(self):
        self.pin.chmod(0o644)
        with self.assertRaisesRegex(ValueError, "private"):
            self.inspect()
        self.pin.unlink()
        protected = self.root / "protected.sha"
        protected.write_text(self.old + "\n", encoding="ascii")
        protected.chmod(0o600)
        self.pin.symlink_to(protected)
        with self.assertRaisesRegex(ValueError, "symlinks"):
            self.inspect()


if __name__ == "__main__":
    unittest.main()
