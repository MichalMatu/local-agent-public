"""Offline, deny-first contracts for the macOS MVP readiness gate."""
from __future__ import annotations

import os
import stat
import subprocess
import unittest
from argparse import Namespace
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest import mock

from local_agent.cli import host_remote as cli
from local_agent.supervisor import host_mvp_readiness as readiness


def _stat(mode: int, uid: int = 0) -> os.stat_result:
    return os.stat_result((mode, 0, 0, 1, uid, 0, 0, 0, 0, 0))


class MacOwnerMvpReadinessTests(unittest.TestCase):
    def test_non_macos_does_not_invoke_sudo(self):
        with mock.patch.object(readiness.sys, "platform", "linux"), mock.patch.object(
            readiness.subprocess, "run",
        ) as run:
            self.assertEqual(readiness.inspect_macos_helper_risk()["status"],
                             "unsupported_platform")
            run.assert_not_called()

    def test_sudo_unavailable_fails_closed(self):
        with mock.patch.object(readiness.sys, "platform", "darwin"), mock.patch.object(
            readiness.subprocess, "run", side_effect=subprocess.TimeoutExpired("sudo", 10),
        ):
            self.assertEqual(readiness.inspect_macos_helper_risk()["status"],
                             "sudo_policy_unavailable")

    def test_unobserved_helper_rule_does_not_certify_sudo(self):
        completed = subprocess.CompletedProcess(
            ["/usr/bin/sudo", "-n", "-l"], 0, "NOPASSWD: /usr/bin/true\n", "",
        )
        with mock.patch.object(readiness.sys, "platform", "darwin"), mock.patch.object(
            readiness.subprocess, "run", return_value=completed,
        ):
            self.assertEqual(readiness.inspect_macos_helper_risk()["status"],
                             "sudo_helper_rule_unverified")

    def test_writable_sudo_helper_ancestor_is_blocked(self):
        completed = subprocess.CompletedProcess(
            ["/usr/bin/sudo", "-n", "-l"], 0,
            "NOPASSWD: /usr/local/sbin/local-agent-powermetrics\n", "",
        )

        def metadata(path: Path) -> os.stat_result:
            if path == readiness._HELPER:
                return _stat(stat.S_IFREG | 0o755)
            if path == Path("/usr/local/sbin"):
                return _stat(stat.S_IFDIR | 0o775, uid=501)
            return _stat(stat.S_IFDIR | 0o755)

        with mock.patch.object(readiness.sys, "platform", "darwin"), mock.patch.object(
            readiness.subprocess, "run", return_value=completed,
        ) as run, mock.patch.object(
            Path, "lstat", autospec=True, side_effect=metadata,
        ), mock.patch.object(readiness.os, "access", return_value=False):
            report = readiness.inspect_macos_helper_risk()
            self.assertEqual(report, {
                "status": "unsafe_writable_ancestor", "path": "/usr/local/sbin",
            })
            self.assertEqual(run.call_args.args[0], ["/usr/bin/sudo", "-n", "-l"])

    def test_posix_clean_path_still_does_not_prove_full_privilege_safety(self):
        completed = subprocess.CompletedProcess(
            [], 0, "NOPASSWD: /usr/local/sbin/local-agent-powermetrics\n", "",
        )

        def metadata(path: Path) -> os.stat_result:
            return _stat(stat.S_IFREG | 0o755 if path == readiness._HELPER
                         else stat.S_IFDIR | 0o755)

        with mock.patch.object(readiness.sys, "platform", "darwin"), mock.patch.object(
            readiness.subprocess, "run", return_value=completed,
        ), mock.patch.object(Path, "lstat", autospec=True, side_effect=metadata), (
            mock.patch.object(readiness.os, "access", return_value=False)
        ):
            report = readiness.inspect_macos_helper_risk()
            self.assertEqual(report["status"], "posix_path_protected_only")
            self.assertFalse(report["sudo_scope_verified"])

    def test_even_valid_root_state_never_enables_standing(self):
        with mock.patch.object(
            readiness, "inspect_macos_helper_risk",
            return_value={"status": "posix_path_protected_only"},
        ), mock.patch.object(
            readiness, "inspect_root_owned_pairing",
            return_value={"paired": True, "epoch": 1},
        ):
            report = readiness.assess_owner_mvp_readiness(
                record_path=Path("/Library/owner-pairing.json"),
                identity_file=Path("/tmp/id"), expected_repository="owner/repo",
            )
        self.assertFalse(report["standing_execution_enabled"])
        self.assertFalse(report["github_writers_verified"])
        self.assertEqual(report["execution_authority"], "not_asserted")
        self.assertIn("full_privilege_and_acl_audit_not_completed", report["blockers"])

    def test_malformed_or_missing_root_state_is_blocked(self):
        with mock.patch.object(
            readiness, "inspect_macos_helper_risk",
            return_value={"status": "unsafe_writable_ancestor"},
        ), mock.patch.object(
            readiness, "inspect_root_owned_pairing",
            side_effect=ValueError("missing"),
        ):
            report = readiness.assess_owner_mvp_readiness(
                record_path=Path("/Library/owner-pairing.json"),
                identity_file=Path("/tmp/id"), expected_repository="owner/repo",
            )
        self.assertEqual(report["protected_owner_state"]["status"],
                         "root_state_unverified")
        self.assertIn("protected_local_owner_pairing_not_verified", report["blockers"])
        self.assertIn("sudo_helper_has_service_writable_ancestor", report["blockers"])

    def test_cli_is_diagnostic_only_and_returns_nonzero(self):
        payload = {"status": "blocked_for_standing_owner_mvp",
                   "standing_execution_enabled": False,
                   "execution_authority": "not_asserted"}
        buffer = StringIO()
        with mock.patch.object(
            cli, "assess_owner_mvp_readiness", return_value=payload,
        ), redirect_stdout(buffer):
            status = cli.command_mvp_readiness(Namespace(
                record_path=Path("/Library/owner-pairing.json"),
                identity_file=Path("/tmp/id"), repository="owner/repo",
            ))
        self.assertEqual(status, 2)
        self.assertIn("blocked_for_standing_owner_mvp", buffer.getvalue())


if __name__ == "__main__":
    unittest.main()
