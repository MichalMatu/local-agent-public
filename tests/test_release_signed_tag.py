"""Offline signed-tag release gate regressions; no signing key or network needed."""
from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scripts.release import verify_signed_tag


class SignedTagTests(unittest.TestCase):
    def test_rejects_malformed_tag_and_commit(self) -> None:
        self.assertEqual(
            verify_signed_tag.check_signed_tag(Path("."), "--all", "a" * 40)["status"],
            "FAIL",
        )
        self.assertEqual(
            verify_signed_tag.check_signed_tag(Path("."), "v4.20.15", "HEAD")["status"],
            "FAIL",
        )

    def test_no_repository_is_unverified(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            result = verify_signed_tag.check_signed_tag(
                Path(directory), "v4.20.15", "a" * 40,
            )
        self.assertEqual(result["status"], "UNVERIFIED")

    def test_annotated_unsigned_tag_fails_real_git_verification(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            def git(*args: str) -> str:
                result = subprocess.run(
                    ["git", "-C", str(root), *args],
                    check=True, capture_output=True, text=True, timeout=10,
                )
                return result.stdout.strip()
            git("init", "-q")
            git("config", "user.name", "Test Maintainer")
            git("config", "user.email", "test@local.invalid")
            git("config", "commit.gpgsign", "false")
            git("commit", "--allow-empty", "-m", "source")
            sha = git("rev-parse", "HEAD")
            git("tag", "-a", "v4.20.15", "-m", "unsigned")
            result = verify_signed_tag.check_signed_tag(root, "v4.20.15", sha)
            self.assertEqual(result, {
                "status": "FAIL", "reason": "tag_signature_invalid_or_missing",
            })
            result = verify_signed_tag.check_signed_tag(root, "v4.20.15", "a" * 40)
            self.assertEqual(result["reason"], "source_head_mismatch")

    def test_valid_signature_and_pinned_target_pass_verifier(self) -> None:
        root = Path("/tmp/source")
        sha = "b" * 40
        def probe(_root, *args):
            if args == ("rev-parse", "--show-toplevel"):
                return 0, str(root)
            if args == ("rev-parse", "HEAD^{commit}"):
                return 0, sha
            if args == ("cat-file", "-t", "refs/tags/v4.20.15"):
                return 0, "tag"
            if args == ("rev-parse", "refs/tags/v4.20.15^{commit}"):
                return 0, sha
            if args == ("verify-tag", "refs/tags/v4.20.15"):
                return 0, ""
            raise AssertionError(args)
        with mock.patch.object(verify_signed_tag, "_git", side_effect=probe) as call:
            result = verify_signed_tag.check_signed_tag(root, "v4.20.15", sha)
        self.assertEqual(result["status"], "PASS")
        self.assertEqual(call.call_count, 5)


if __name__ == "__main__":
    unittest.main()
