"""Offline tests of guarded *local administrator* privileged path remediation."""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from argparse import Namespace
from contextlib import contextmanager, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest import mock

from local_agent.cli import host_remote as cli
from local_agent.supervisor import host_privileged_helper_repair as repair


class AdminHelperRepairTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.original = self.root / "sbin"
        self.original.mkdir(mode=0o775)
        self.original.chmod(0o775)
        self.helper = self.original / "local-agent-powermetrics"
        self.helper.write_bytes(b"offline-fixture-only")
        self.helper.chmod(0o755)
        self.journal_dir = self.root / "journal"
        self.journal_dir.mkdir()
        self.sentinel = self.journal_dir / "untouched"
        self.sentinel.write_text("owner data")

    def change(self, **overrides):
        kwargs = {
            "expected_uid": os.getuid(),
            "expected_mode": 0o775,
            "confirmation": f"PROTECT /usr/local/sbin {os.getuid()} 0775",
            "reviewed_shared_directory": True,
            "journal_record": self.journal_dir / "owner-pairing.json",
        }
        return repair.protect_sudo_helper_parent(**(kwargs | overrides))

    @contextmanager
    def fake_root_dir(self, record_path: Path, *, create: bool):
        del record_path, create
        fd = os.open(self.journal_dir, os.O_RDONLY)
        try:
            yield fd
        finally:
            os.close(fd)

    def test_denies_unreviewed_shared_directory_before_effects(self):
        with mock.patch.object(repair, "_require_local_admin"):
            with self.assertRaisesRegex(ValueError, "review shared"):
                self.change(reviewed_shared_directory=False)
        self.assertFalse((self.journal_dir / repair._JOURNAL).exists())

    def test_denies_wrong_confirmation_and_expected_mode(self):
        with mock.patch.object(repair, "_require_local_admin"):
            with self.assertRaisesRegex(ValueError, "exact local"):
                self.change(confirmation="yes")
            with self.assertRaisesRegex(ValueError, "owner/mode"):
                self.change(expected_mode=0o755)

    def test_protected_repair_journals_before_any_mutation(self):
        def inspect():
            return os.open(self.original, os.O_RDONLY), self.original.stat(), self.helper.stat()

        with mock.patch.object(repair, "_require_local_admin"), mock.patch.object(
            repair, "_inspect_target", side_effect=inspect,
        ), mock.patch.object(repair, "_protected_directory", side_effect=self.fake_root_dir), (
            mock.patch.object(repair, "_require_root_only")
        ), mock.patch.object(repair, "_require_no_macos_acl"), mock.patch.object(
            repair.os, "fchmod",
        ) as chmod, mock.patch.object(repair.os, "fchown") as chown, mock.patch.object(
            repair, "_HELPER", self.helper,
        ):
            timeline = mock.Mock()
            timeline.attach_mock(chmod, "chmod")
            timeline.attach_mock(chown, "chown")
            output = self.change()
            self.assertEqual(timeline.mock_calls, [
                mock.call.chown(mock.ANY, 0, 0),
                mock.call.chmod(mock.ANY, 0o755),
            ])
            self.assertEqual(output["status"], "helper_parent_protected")
            self.assertFalse(output["standing_execution_enabled"])
            self.assertEqual(output["execution_authority"], "not_asserted")
            evidence = json.loads((self.journal_dir / repair._JOURNAL).read_text())
            self.assertEqual(evidence["status"], "prepared_before_change")
            self.assertEqual(evidence["original_mode"], 0o775)
            self.assertEqual(evidence["original_uid"], os.getuid())
            self.assertEqual(chmod.call_count, 1)
            chown.assert_called_once()
            self.assertEqual(self.sentinel.read_text(), "owner data")
            # A second invocation MUST fail at immutable backup (no replay).
            with self.assertRaises(FileExistsError):
                self.change()
            self.assertEqual(chmod.call_count, 1)

    def test_failed_chown_keeps_prechange_journal_without_chmod(self):
        def inspect():
            return os.open(self.original, os.O_RDONLY), self.original.stat(), self.helper.stat()

        with (
            mock.patch.object(repair, "_require_local_admin"),
            mock.patch.object(repair, "_inspect_target", side_effect=inspect),
            mock.patch.object(repair, "_protected_directory", side_effect=self.fake_root_dir),
            mock.patch.object(repair.os, "fchmod") as chmod,
            mock.patch.object(repair.os, "fchown", side_effect=OSError("simulated chown failure")),
        ):
            with self.assertRaisesRegex(OSError, "simulated chown failure"):
                self.change()
            chmod.assert_not_called()
            journal = json.loads((self.journal_dir / repair._JOURNAL).read_text())
            self.assertEqual(journal["status"], "prepared_before_change")
            self.assertEqual(journal["original_mode"], 0o775)

    def test_service_user_in_wheel_denied_before_journal(self):
        with (
            mock.patch.object(repair, "_require_local_admin"),
            mock.patch.object(repair.pwd, "getpwuid", return_value=mock.Mock(
                pw_name="agent-service", pw_gid=20,
            )),
            mock.patch.object(repair.os, "getgrouplist", return_value=[20, 0]),
        ):
            with self.assertRaisesRegex(ValueError, "service account is in wheel"):
                self.change()
        self.assertFalse((self.journal_dir / repair._JOURNAL).exists())

    def test_changed_parent_fails_before_journal_or_fix(self):
        def inspect():
            info = list(self.original.stat())
            info[4] = 1234
            return os.open(self.original, os.O_RDONLY), os.stat_result(info), self.helper.stat()

        with mock.patch.object(repair, "_require_local_admin"), mock.patch.object(
            repair, "_inspect_target", side_effect=inspect,
        ):
            with self.assertRaisesRegex(ValueError, "changed since"):
                self.change()
        self.assertFalse((self.journal_dir / repair._JOURNAL).exists())

    def test_cli_denies_nonroot_without_tty_or_mutation(self):
        out = StringIO()
        with (
            mock.patch.object(cli, "_require_local_admin",
                              side_effect=ValueError("nonroot")),
            mock.patch.object(cli, "_interactive_local_confirmation") as confirm,
            mock.patch.object(cli, "protect_sudo_helper_parent") as mutate,
            redirect_stdout(out),
        ):
            result = cli.command_owner_admin_protect_helper(Namespace(
                expected_uid=501, expected_mode=0o775, reviewed_shared_directory=True,
            ))
        self.assertEqual(result, 2)
        confirm.assert_not_called()
        mutate.assert_not_called()
        self.assertIn("repair_outcome_unverified", out.getvalue())


if __name__ == "__main__":
    unittest.main()
