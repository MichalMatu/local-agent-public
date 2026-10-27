"""Read-only GitHub owner-pairing plan on a disposable real Git checkout.

The only mocked operation is the remote GitHub ls-remote response: tests
never access user credentials, production refs, network or service state.
"""
from __future__ import annotations

import contextlib
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from local_agent.repository.host_facts import initialize_local_host_identity
from local_agent.supervisor import host_pairing_plan as pairing


class PrivateGithubPairingPlanTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="la-pairing-preflight-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.private = self.root / "identity"
        self.private.mkdir(mode=0o700)
        self.identity_file = self.private / "identity.json"
        self.identity = initialize_local_host_identity(self.identity_file, "owner-test-mac")
        self.checkouts = self.root / "checkouts"
        self.checkouts.mkdir(mode=0o700)
        self.checkout = self.checkouts / "control"
        self.checkout.mkdir(mode=0o700)
        self._git("init", "-q", "-b", "agent-control")
        self._git("config", "--local", "user.name", "Fixture User")
        self._git("config", "--local", "user.email", "fixture@example.invalid")
        (self.checkout / "README.md").write_text("offline fixture\n")
        self._git("add", "--", "README.md")
        self._git("commit", "-qm", "fixture initial commit")
        self._git("remote", "add", "origin", "git@github.com:ExampleOwner/paired-host.git")
        self.sha = self._git("rev-parse", "HEAD")
        self.args = {
            "identity_file": self.identity_file,
            "control_checkout": self.checkout,
            "repository": "ExampleOwner/paired-host",
            "expected_host_id": self.identity.host_id,
            "expected_installation_id": self.identity.installation_id,
            "expected_control_sha": self.sha,
        }

    def _git(self, *args: str) -> str:
        process = subprocess.run(
            ["git", *args], cwd=self.checkout, capture_output=True, text=True,
            check=True, timeout=10,
            env={**os.environ, "GIT_CONFIG_NOSYSTEM": "1",
                 "GIT_CONFIG_GLOBAL": os.devnull},
        )
        return process.stdout.strip()

    @contextlib.contextmanager
    def remote_fixture(self, *, sha: str | None = None, rewrite: bool = False):
        original = pairing._git
        def read(cwd: Path, *args: str, limit: int = 8192) -> str:
            if args[:2] == ("ls-remote", "--heads"):
                return f"{self.sha if sha is None else sha}\trefs/heads/agent-control\n"
            if rewrite and args[:3] == ("remote", "get-url", "origin"):
                return "https://github.com/ExampleOwner/paired-host.git\n"
            return original(cwd, *args, limit=limit)
        with mock.patch.object(pairing, "_git", side_effect=read):
            yield

    def test_valid_read_only_plan_is_explicitly_not_authorization(self) -> None:
        before = (self._git("rev-parse", "HEAD"), self._git("status", "--porcelain"))
        with self.remote_fixture():
            result = pairing.plan_private_github_pairing(**self.args)
        self.assertEqual(result["status"], "source_preflight_pass")
        self.assertEqual(result["repository"], "exampleowner/paired-host")
        self.assertEqual(result["host_id"], self.identity.host_id)
        self.assertEqual(result["installation_id"], self.identity.installation_id)
        self.assertEqual(result["control_sha"], self.sha)
        self.assertFalse(result["paired"])
        self.assertFalse(result["published"])
        self.assertFalse(result["github_writers_verified"])
        self.assertTrue(result["owner_confirmation_required"])
        self.assertEqual(result["execution_authority"], "not_asserted")
        self.assertEqual(before, (self._git("rev-parse", "HEAD"),
                                  self._git("status", "--porcelain")))

    def test_unexpected_target_identity_never_gets_a_plan(self) -> None:
        for override in (
            {"expected_host_id": "different-mac"},
            {"expected_installation_id": "3a649fc9-5577-485a-89c2-29f7f02a8721"},
        ):
            with self.subTest(override=override), self.remote_fixture():
                with self.assertRaisesRegex(ValueError, "installation is different"):
                    pairing.plan_private_github_pairing(**{**self.args, **override})

    def test_private_repository_selected_explicitly_and_url_must_match(self) -> None:
        for repository in ("other/paired-host", "ExampleOwner/other", "../paired-host",
                           "ExampleOwner/paired-host.git", "ExampleOwner/\nprivate"):
            with self.subTest(repository=repository), self.remote_fixture():
                with self.assertRaises(ValueError):
                    pairing.plan_private_github_pairing(**{**self.args, "repository": repository})

    def test_no_credential_url_or_cross_origin_trust(self) -> None:
        for origin in (
            "https://user:token@github.com/ExampleOwner/paired-host.git",
            "https://github.example.com/ExampleOwner/paired-host.git",
            "file:///tmp/paired-host.git",
            "ssh://bad@github.com/ExampleOwner/paired-host",
            "git@github.com:other/paired-host.git",
        ):
            with self.subTest(origin=origin):
                self._git("remote", "set-url", "origin", origin)
                with self.remote_fixture(), self.assertRaises(ValueError):
                    pairing.plan_private_github_pairing(**self.args)
        self._git("remote", "set-url", "origin", "git@github.com:ExampleOwner/paired-host.git")

    def test_effective_origin_rewrite_does_not_silently_pair(self) -> None:
        with self.remote_fixture(rewrite=True), self.assertRaisesRegex(
            ValueError, "rewritten effective Git origins",
        ):
            pairing.plan_private_github_pairing(**self.args)

    def test_dirty_branch_and_mismatched_sha_denied_without_cleanup(self) -> None:
        extra = self.checkout / "owner-untracked.txt"
        extra.write_text("never delete me\n")
        with self.remote_fixture(), self.assertRaisesRegex(ValueError, "dirty or untracked"):
            pairing.plan_private_github_pairing(**self.args)
        self.assertEqual(extra.read_text(), "never delete me\n")
        extra.unlink()
        self._git("checkout", "-qb", "other")
        with self.remote_fixture(), self.assertRaisesRegex(ValueError, "agent-control"):
            pairing.plan_private_github_pairing(**self.args)
        self._git("checkout", "-q", "agent-control")
        with self.remote_fixture(sha="f" * 40), self.assertRaisesRegex(
            ValueError, "exact pinned",
        ):
            pairing.plan_private_github_pairing(**self.args)
        with self.remote_fixture(), self.assertRaisesRegex(ValueError, "exact pinned"):
            pairing.plan_private_github_pairing(
                **{**self.args, "expected_control_sha": "0" * 40}
            )

    def test_untrusted_private_directory_or_git_identity_denied(self) -> None:
        self.checkouts.chmod(0o755)
        try:
            with self.remote_fixture(), self.assertRaisesRegex(ValueError, "0700"):
                pairing.plan_private_github_pairing(**self.args)
        finally:
            self.checkouts.chmod(0o700)
        git_identity = self.checkout / "identity.json"
        git_identity.write_bytes(self.identity_file.read_bytes())
        git_identity.chmod(0o600)
        try:
            with self.remote_fixture(), self.assertRaises(ValueError):
                pairing.plan_private_github_pairing(
                    **{**self.args, "identity_file": git_identity},
                )
        finally:
            git_identity.unlink()

    def test_missing_remote_and_wrong_git_state_refuse(self) -> None:
        with mock.patch.object(pairing, "_git", side_effect=ValueError("offline")):
            with self.assertRaises(ValueError):
                pairing.plan_private_github_pairing(**self.args)
        with self.remote_fixture(), self.assertRaises(ValueError):
            pairing.plan_private_github_pairing(
                **{**self.args, "expected_control_sha": "BAD"}
            )

    def test_cli_is_read_only_and_reports_no_authority(self) -> None:
        from argparse import Namespace
        from io import StringIO
        from contextlib import redirect_stdout
        from local_agent.cli.host_remote import command_pairing_plan

        with self.remote_fixture():
            out = StringIO()
            with redirect_stdout(out):
                exit_code = command_pairing_plan(Namespace(**self.args))
            self.assertEqual(exit_code, 0)
            data = json.loads(out.getvalue())
            self.assertFalse(data["github_writers_verified"])
            self.assertEqual(data["execution_authority"], "not_asserted")
        out = StringIO()
        with self.remote_fixture(), redirect_stdout(out):
            exit_code = command_pairing_plan(
                Namespace(**{**self.args, "repository": "other/project"}),
            )
        self.assertEqual(exit_code, 2)
        self.assertEqual(json.loads(out.getvalue())["execution_authority"], "not_asserted")


if __name__ == "__main__":
    unittest.main()
