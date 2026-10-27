"""Safe source-only security scanner integration and negative fixtures."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scripts.quality import security_audit


class SecurityAuditTests(unittest.TestCase):
    def test_all_real_requirements_are_exactly_pinned(self) -> None:
        root = Path(__file__).resolve().parents[1]
        result = security_audit.manifest_report(root)
        self.assertEqual(result["status"], "pass")
        self.assertFalse(result["vulnerability_database_checked"])

    def test_unpinned_or_url_dependency_reports_location_not_contents(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            for item in security_audit.MANIFESTS:
                path = root / item
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("example==1.2.3\n", encoding="utf-8")
            (root / security_audit.MANIFESTS[0]).write_text(
                "example>=1.2\nhttps://user:veryprivate@example.invalid/pkg\n",
                encoding="utf-8",
            )
            result = security_audit.manifest_report(root)
            self.assertEqual(result["status"], "fail")
            self.assertEqual(len(result["issues"]), 2)
            self.assertNotIn("veryprivate", repr(result))

    def test_public_source_has_no_historical_secret_exemptions(self) -> None:
        root = Path(__file__).resolve().parents[1]
        ignore = (root / ".gitleaksignore").read_text(encoding="utf-8")
        exceptions = [line.strip() for line in ignore.splitlines()
                      if line.strip() and not line.lstrip().startswith("#")]
        self.assertEqual(exceptions, [])

    def test_missing_gitleaks_is_unverified_not_pass(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(security_audit.shutil, "which", return_value=None):
                result = security_audit.secrets_report(Path(tmp).resolve())
        self.assertEqual(result["status"], "unverified")
        self.assertIsNone(result["findings"])

    def test_scanner_has_full_redaction_and_never_returns_secret_content(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            def fake_scan(args, **kwargs):
                path = Path(args[args.index("--report-path") + 1])
                path.write_text(
                    '[{"Secret":"VERY-SENSITIVE-MOCK-VALUE","File":"fake.py"}]',
                    encoding="utf-8",
                )
                self.assertIn("--redact=100", args)
                self.assertIn("--max-archive-depth=0", args)
                self.assertEqual(args[-1], ".")
                self.assertEqual(kwargs["cwd"], root)
                return mock.Mock(returncode=1, stdout=b"secret", stderr=b"secret")
            with mock.patch.object(security_audit.subprocess, "run", side_effect=fake_scan):
                result = security_audit.secrets_report(root, command="/path/to/gitleaks")
            self.assertEqual(result, {
                "status": "fail", "scanner": "gitleaks", "findings": 1
            })
            self.assertNotIn("VERY-SENSITIVE-MOCK-VALUE", repr(result))

    def test_ambiguous_scanner_failure_is_not_reported_as_clean(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            with mock.patch.object(security_audit.subprocess, "run",
                                   return_value=mock.Mock(returncode=3)):
                result = security_audit.secrets_report(root, command="gitleaks")
            self.assertEqual(result["status"], "unverified")

    def test_history_requires_full_git_checkout(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            with mock.patch.object(security_audit.shutil, "which", return_value="gitleaks"):
                result = security_audit.secrets_report(root, history=True)
            self.assertEqual(result["status"], "unverified")
            self.assertEqual(result["reason"], "git_history_unavailable")

    def test_shallow_history_is_unverified_without_scanning(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            def git_probe(args, **_kwargs):
                if args[-1] == "--show-toplevel":
                    return mock.Mock(returncode=0, stdout=str(root))
                return mock.Mock(returncode=0, stdout="true")
            with mock.patch.object(security_audit.subprocess, "run",
                                   side_effect=git_probe) as run:
                result = security_audit.secrets_report(
                    root, command="gitleaks", history=True,
                )
            self.assertEqual(result["status"], "unverified")
            self.assertEqual(result["reason"], "shallow_history")
            self.assertEqual(run.call_count, 2)

    def test_history_scan_uses_all_reachable_refs_with_redaction(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            def scan(args, **kwargs):
                if args[0] == "git":
                    if args[-1] == "--show-toplevel":
                        return mock.Mock(returncode=0, stdout=str(root))
                    return mock.Mock(returncode=0, stdout="false")
                self.assertEqual(args[0:2], ["gitleaks", "git"])
                self.assertIn("--log-opts=--all --full-history", args)
                self.assertIn("--redact=100", args)
                self.assertEqual(kwargs["cwd"], root)
                report = Path(args[args.index("--report-path") + 1])
                report.write_text("[]", encoding="utf-8")
                return mock.Mock(returncode=0)
            with mock.patch.object(security_audit.subprocess, "run", side_effect=scan):
                result = security_audit.secrets_report(
                    root, command="gitleaks", history=True,
                )
            self.assertEqual(result["status"], "pass")
            self.assertEqual(result["findings"], 0)

    def test_history_mode_failure_is_not_silently_clean(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            (root / "local_agent").mkdir()
            with mock.patch.object(security_audit.shutil, "which",
                                   return_value="gitleaks"):
                result = security_audit.run(root, mode="history")
            self.assertEqual(result["status"], "unverified")
            self.assertIn("history", result["checks"])

    def test_missing_repo_is_unverified(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(
                security_audit.run(Path(tmp).resolve(), mode="manifests")["status"],
                "unverified",
            )


if __name__ == "__main__":
    unittest.main()
