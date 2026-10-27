"""Deny-first tests for read-only root-owned macOS pairing boundary."""
from __future__ import annotations

import json
import os
import stat
import tempfile
import unittest
from argparse import Namespace
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest import mock

from local_agent.supervisor import host_pairing_boundary as boundary
from local_agent.cli import host_remote as cli


class RootOwnedPairingBoundaryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.identity = self.root / "identity.json"
        self.identity.write_text(json.dumps({
            "version": 1, "host_id": "owner-mac",
            "installation_id": "b0e94f93-478f-4fc6-abed-159d3702362f",
        }))
        self.identity.chmod(0o600)
        self.record = self.root / "owner-pairing.json"
        self.data = {
            "version": 1, "state": "paired", "repository": "owner/host-inbox",
            "host_id": "owner-mac",
            "installation_id": "b0e94f93-478f-4fc6-abed-159d3702362f",
            "namespace": (
                ".agent/hosts/owner-mac/supervisor/"
                "b0e94f93-478f-4fc6-abed-159d3702362f"
            ),
            "epoch": 1, "control_sha": "a" * 40,
            "standing_execution_enabled": False,
        }
        self.record.write_text(json.dumps(self.data))
        self.record.chmod(0o644)

    def inspect(self, **changes):
        args = {
            "record_path": self.record, "identity_file": self.identity,
            "expected_repository": "owner/host-inbox",
        }
        return boundary.inspect_root_owned_pairing(**(args | changes))

    def test_non_macos_refused_without_filesystem_effects(self):
        with mock.patch.object(boundary.sys, "platform", "linux"):
            with self.assertRaisesRegex(ValueError, "macOS-only"):
                self.inspect()
        self.assertEqual(json.loads(self.record.read_text()), self.data)

    def test_non_root_writable_fixture_denied_on_macos(self):
        with mock.patch.object(boundary.sys, "platform", "darwin"):
            with mock.patch.object(boundary, "_require_no_macos_acl"):
                with self.assertRaisesRegex(ValueError, "root-only"):
                    self.inspect()

    def test_file_name_symlink_and_relative_paths_denied(self):
        with mock.patch.object(boundary.sys, "platform", "darwin"):
            for path in [Path("relative/owner-pairing.json"), self.identity, Path("/")]:
                with self.subTest(path=path), self.assertRaises(ValueError):
                    self.inspect(record_path=path)
            link_dir = self.root / "link"
            link_dir.symlink_to(self.root, target_is_directory=True)
            link = link_dir / "owner-pairing.json"
            with self.assertRaises(OSError), mock.patch.object(
                boundary, "_require_root_only",
            ), mock.patch.object(boundary, "_require_no_macos_acl"):
                self.inspect(record_path=link)

    def test_root_only_check_rejects_group_writes_and_links(self):
        original = self.record.stat()
        values = list(original)
        values[4] = 0
        values[0] = stat.S_IFREG | 0o664
        with self.assertRaisesRegex(ValueError, "root-only"):
            boundary._require_root_only(os.stat_result(values), directory=False)
        values[0] = stat.S_IFREG | 0o644
        values[3] = 2
        with self.assertRaisesRegex(ValueError, "root-only"):
            boundary._require_root_only(os.stat_result(values), directory=False)

    def test_acl_marked_or_unavailable_refused(self):
        for output in ["drwxr-xr-x+ 3 root wheel 96 Oct 17 17:00 /Library\n",
                       "drwxr-xr-x@ 3 root wheel 96 Oct 17 17:00 /Library\n"
                       " 0: user:test allow write\n"]:
            completed = mock.Mock(returncode=0, stderr="", stdout=output)
            with mock.patch.object(boundary.subprocess, "run", return_value=completed):
                with self.assertRaisesRegex(ValueError, "ACL"):
                    boundary._require_no_macos_acl(Path("/Library"))
        with mock.patch.object(boundary.subprocess, "run", side_effect=FileNotFoundError):
            with self.assertRaises(OSError):
                boundary._require_no_macos_acl(Path("/Library"))

    def test_simulated_root_only_snapshot_is_read_only_not_authority(self):
        """Ownership is simulated: not a proof of root-installed state on test Mac."""
        real_fstat = boundary.os.fstat

        def pretend_root(fd):
            metadata = list(real_fstat(fd))
            metadata[4] = 0
            metadata[0] &= ~0o022
            return os.stat_result(metadata)

        with mock.patch.object(boundary.sys, "platform", "darwin"), (
            mock.patch.object(boundary.os, "fstat", side_effect=pretend_root)
        ), mock.patch.object(boundary, "_require_no_macos_acl"):
            observed = self.inspect()
            self.assertTrue(observed["paired"])
            self.assertEqual(observed["macos_root_write_boundary"], "observed")
            self.assertEqual(observed["execution_authority"], "not_asserted")
            self.assertFalse(observed["github_writers_verified"])
            self.assertFalse(observed["standing_execution_enabled"])
            with self.assertRaisesRegex(ValueError, "does not match"):
                self.inspect(expected_repository="another/repo")
            original = self.data.copy()
            self.data["standing_execution_enabled"] = True
            self.record.write_text(json.dumps(self.data))
            with self.assertRaisesRegex(ValueError, "cannot enable standing"):
                self.inspect()
            self.record.write_text(json.dumps(original))

    def test_cli_failed_observation_is_never_authority(self):
        out = StringIO()
        with mock.patch.object(boundary.sys, "platform", "darwin"):
            with redirect_stdout(out):
                result = cli.command_pair_boundary_check(Namespace(
                    record_path=self.record, identity_file=self.identity,
                    repository="owner/host-inbox",
                ))
        self.assertEqual(result, 2)
        payload = json.loads(out.getvalue())
        self.assertEqual(payload["status"], "unverified")
        self.assertFalse(payload["standing_execution_enabled"])
        self.assertEqual(payload["execution_authority"], "not_asserted")

    def test_no_remote_worker_wiring(self):
        source = Path(boundary.__file__).read_text()
        self.assertNotIn("require_exact_host_command_grant", source)
        self.assertNotIn("allow_execution", source)


if __name__ == "__main__":
    unittest.main()
