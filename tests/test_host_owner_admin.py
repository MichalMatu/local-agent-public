"""Root-owned local owner-choice lifecycle: deny first, offline and inert."""
from __future__ import annotations

import json
import stat
import tempfile
import unittest
from argparse import Namespace
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest import mock

from local_agent.cli import host_remote as cli
from local_agent.supervisor import host_owner_admin as admin

_UUID = "b0e94f93-478f-4fc6-abed-159d3702362f"
_SHA = "a" * 40


class RootOwnerAdminTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.identity = self.root / "identity.json"
        self.identity.write_text(json.dumps({
            "version": 1, "host_id": "test-mac",
            "installation_id": _UUID,
        }))
        self.identity.chmod(0o600)
        self.record = self.root / "LocalAgent" / "owner-pairing.json"

    def change(self, action: str, **changes):
        data = {
            "action": action, "record_path": self.record,
            "identity_file": self.identity, "repository": "owner/host",
            "expected_host_id": "test-mac", "expected_installation_id": _UUID,
            "expected_control_sha": _SHA if action == "pair" else None,
            "typed_confirmation": (f"PAIR owner/host test-mac {_UUID}" if action == "pair"
                                   else f"UNPAIR owner/host test-mac {_UUID}"),
        }
        return admin.change_protected_owner_choice(**(data | changes))

    def simulated_root(self):
        """Ownership/ACL mocked only to test mechanics; NOT a live Mac acceptance."""
        return (
            mock.patch.object(admin, "_require_local_admin"),
            mock.patch.object(admin, "_require_privileged_helper_path_protected"),
            mock.patch.object(admin, "_require_root_only"),
            mock.patch.object(admin, "_require_no_macos_acl"),
        )

    def test_unprivileged_and_non_macos_rejected(self):
        with mock.patch.object(admin.sys, "platform", "darwin"), mock.patch.object(
            admin.os, "geteuid", return_value=501,
        ):
            with self.assertRaisesRegex(ValueError, "root administrator"):
                self.change("pair")
        with mock.patch.object(admin.sys, "platform", "linux"), mock.patch.object(
            admin.os, "geteuid", return_value=0,
        ):
            with self.assertRaisesRegex(ValueError, "root administrator"):
                self.change("pair")
        self.assertFalse(self.record.exists())

    def test_obvious_nonlocal_terminal_rejected(self):
        with mock.patch.object(admin.sys, "platform", "darwin"), mock.patch.object(
            admin.os, "geteuid", return_value=0,
        ), mock.patch.object(admin.sys.stdin, "isatty", return_value=True), (
            mock.patch.object(admin.sys.stdout, "isatty", return_value=True)
        ), mock.patch.dict(admin.os.environ, {"SSH_TTY": "/dev/ttys999"}):
            with self.assertRaisesRegex(ValueError, "local interactive"):
                self.change("pair")
        self.assertFalse(self.record.exists())

    def test_insecure_privileged_helper_blocks_pair_but_not_unpair(self):
        with mock.patch.object(admin, "_require_local_admin"), mock.patch.object(
            admin, "_require_privileged_helper_path_protected",
            side_effect=ValueError("unsafe helper directory"),
        ) as gate:
            with self.assertRaisesRegex(ValueError, "unsafe helper"):
                self.change("pair")
            gate.assert_called_once()
        self.assertFalse(self.record.exists())

    def test_pair_revoke_repair_epoch_tombstone(self):
        a, b, c, d = self.simulated_root()
        with a, b, c, d:
            first = self.change("pair")
            self.assertEqual(first["status"], "protected_owner_choice_recorded")
            self.assertEqual(first["epoch"], 1)
            self.assertFalse(first["standing_execution_enabled"])
            self.assertEqual(first["execution_authority"], "not_asserted")
            self.assertEqual(stat.S_IMODE(self.record.stat().st_mode), 0o644)
            self.assertEqual(stat.S_IMODE(self.record.parent.stat().st_mode), 0o755)
            self.assertEqual(json.loads(self.record.read_text())["state"], "paired")
            with self.assertRaisesRegex(ValueError, "already paired"):
                self.change("pair")
            revoked = self.change("unpair")
            self.assertEqual(revoked["epoch"], 2)
            self.assertFalse(revoked["paired"])
            self.assertEqual(json.loads(self.record.read_text())["state"], "unpaired")
            repeat = self.change("unpair")
            self.assertEqual(repeat["status"], "already_unpaired")
            self.assertEqual(repeat["epoch"], 2)
            repaired = self.change("pair")
            self.assertEqual(repaired["epoch"], 3)
            self.assertEqual(repaired["execution_authority"], "not_asserted")
            self.assertFalse(repaired["standing_execution_enabled"])

    def test_wrong_installation_uuid_confirmation_is_rejected(self):
        a, b, c, d = self.simulated_root()
        with a, b, c, d:
            with self.assertRaisesRegex(ValueError, "confirmation"):
                self.change("pair", typed_confirmation="PAIR owner/host test-mac")
        self.assertFalse(self.record.exists())

    def test_no_missing_parent_for_unpair_and_no_bad_confirmation(self):
        a, b, c, d = self.simulated_root()
        with a, b, c, d:
            with self.assertRaises(FileNotFoundError):
                self.change("unpair")
            with self.assertRaisesRegex(ValueError, "confirmation"):
                self.change("pair", typed_confirmation="anything")
            with self.assertRaisesRegex(ValueError, "exact reviewed"):
                self.change("pair", expected_control_sha="xyz")
        self.assertFalse(self.record.exists())

    def test_foreign_installation_and_repository_cannot_take_over(self):
        a, b, c, d = self.simulated_root()
        with a, b, c, d:
            self.change("pair")
            with self.assertRaisesRegex(ValueError, "another installation/repository"):
                self.change("unpair", repository="attacker/host",
                            typed_confirmation=f"UNPAIR attacker/host test-mac {_UUID}")
            with self.assertRaisesRegex(ValueError, "identity mismatch"):
                self.change("unpair", expected_installation_id="wrong")

    def test_directory_and_record_symlinks_fail_before_write(self):
        a, b, c, d = self.simulated_root()
        with a, b, c, d:
            elsewhere = self.root / "elsewhere"
            elsewhere.mkdir()
            link = self.root / "unsafe"
            link.symlink_to(elsewhere, target_is_directory=True)
            with self.assertRaises(OSError):
                self.change("pair", record_path=link / "owner-pairing.json")
            self.record.parent.mkdir()
            self.record.symlink_to(self.identity)
            with self.assertRaises((OSError, ValueError)):
                self.change("pair")
            self.assertEqual(json.loads(self.identity.read_text())["host_id"], "test-mac")

    def test_existing_invalid_record_never_overwritten(self):
        a, b, c, d = self.simulated_root()
        with a, b, c, d:
            self.record.parent.mkdir()
            self.record.write_text('{"tampered":true}')
            with self.assertRaisesRegex(ValueError, "schema"):
                self.change("pair")
            self.assertEqual(self.record.read_text(), '{"tampered":true}')

    def test_untrusted_python_runtime_is_never_root_approved(self):
        with mock.patch.object(admin.sys, "flags", mock.Mock(isolated=0)):
            with self.assertRaisesRegex(ValueError, "isolated trusted"):
                admin._require_trusted_admin_runtime()
        with mock.patch.object(admin.sys, "flags", mock.Mock(isolated=1)), (
            mock.patch.object(admin, "_require_root_only",
                              side_effect=ValueError("user-writable code"))
        ):
            with self.assertRaisesRegex(ValueError, "user-writable code"):
                admin._require_trusted_admin_runtime()

    def test_cli_nonroot_refuses_without_confirmation(self):
        output = StringIO()
        with (
            mock.patch.object(cli, "_require_local_admin",
                              side_effect=ValueError("not root")),
            mock.patch.object(cli, "_interactive_local_confirmation") as confirm,
            redirect_stdout(output),
        ):
            rc = cli.command_owner_admin_change(Namespace(
                identity_file=self.identity, repository="owner/host",
                admin_action="pair", expected_control_sha=_SHA,
            ))
        self.assertEqual(rc, 2)
        confirm.assert_not_called()
        self.assertIn('"execution_authority": "not_asserted"', output.getvalue())


if __name__ == "__main__":
    unittest.main()
