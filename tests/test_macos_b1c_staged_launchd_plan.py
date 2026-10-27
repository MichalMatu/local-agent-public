"""Source-only security tests for private persistent B1c launchd policy."""
from __future__ import annotations

from pathlib import Path
import plistlib
import tempfile
import unittest
from unittest import mock

from local_agent.platform import macos_launchd
from local_agent.supervisor.macos_release_admission import REQUIRED_ENV
from scripts import macos_b1c_candidate as candidate
from scripts import macos_b1c_staged_launchd_plan as plan
from scripts import macos_b1c_launchd_plan as installed


class MacStagedB1cPlanTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="la-mac-staged-plan-")
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name).resolve()
        self.release = self.home / plan.RELEASE_DIR
        self.source = self.release / plan.SOURCE_DIR
        self.source.mkdir(parents=True, mode=0o700)
        self.release.chmod(0o700)
        (self.source / ".git").mkdir(mode=0o700)
        for path in candidate.PATHS:
            file = self.source / path
            file.parent.mkdir(parents=True, exist_ok=True)
            file.write_text("exact test blob\n")
        (self.source / "agent_entrypoint.py").write_text("original entrypoint\n")
        self.base = self.home / "accepted-B2"
        self.base.mkdir(mode=0o700)
        self.original = plistlib.dumps(macos_launchd.build_launch_agent(
            "parallel", repo_root=self.base, home=self.home, max_workers=2,
        ))
        self.platform = mock.patch.object(plan.sys, "platform", "darwin")
        self.platform.start()
        self.addCleanup(self.platform.stop)
        self.policy = mock.patch.object(
            installed, "_validated_original", return_value=plistlib.loads(self.original),
        )
        self.policy.start()
        self.addCleanup(self.policy.stop)
        self.git = mock.patch.object(plan, "_git", side_effect=self.fake_git)
        self.git.start()
        self.addCleanup(self.git.stop)

    def fake_git(self, source, *args):
        if args == ("remote", "get-url", "origin"):
            return b"https://github.com/MichalMatu/local-agent-public.git"
        if args == ("rev-parse", "HEAD"):
            return plan.EXPECTED_SHA.encode()
        if args == ("rev-parse", "HEAD^"):
            return candidate.BASE_SHA.encode()
        if args == ("status", "--porcelain", "-z"):
            return b""
        if args == ("diff-tree", "--no-commit-id", "--name-only", "-r", "HEAD"):
            return ("\n".join(candidate.PATHS) + "\n").encode()
        if len(args) == 2 and args[0] == "show" and args[1].startswith("HEAD:"):
            return b"exact test blob\n"
        raise AssertionError(f"unexpected Git preflight: {args}")

    def test_disabled_plan_pins_persistent_source_and_required_admission(self):
        result = plan.render_disabled_staged_plan(
            home=self.home, installed_root=self.base, original_plist=self.original,
        )
        doc = plistlib.loads(result)
        self.assertTrue(doc["Disabled"])
        self.assertEqual(doc["ProgramArguments"][1], str(self.source / "agent_entrypoint.py"))
        self.assertEqual(doc["WorkingDirectory"], str(self.source))
        self.assertEqual(doc["EnvironmentVariables"][REQUIRED_ENV], "1")
        self.assertEqual(
            doc["ProgramArguments"][0],
            plistlib.loads(self.original)["ProgramArguments"][0],
        )
        self.assertFalse((self.home / "Library/LaunchAgents").exists())

    def test_refuse_remote_origin_missing(self):
        git = self.fake_git

        def no_origin(source, *args):
            if args == ("remote", "get-url", "origin"):
                return b""
            return git(source, *args)

        with mock.patch.object(plan, "_git", side_effect=no_origin):
            with self.assertRaisesRegex(plan.StagedLaunchdRejected, "absent or untrusted"):
                plan.verify_staged_source(home=self.home)

    def test_refuse_rewritten_fork_origin(self):
        git = self.fake_git

        def rewritten(source, *args):
            if args == ("remote", "get-url", "origin"):
                return b"git@github.com:attacker/local-agent.git"
            return git(source, *args)

        with mock.patch.object(plan, "_git", side_effect=rewritten):
            with self.assertRaisesRegex(plan.StagedLaunchdRejected, "absent or untrusted"):
                plan.verify_staged_source(home=self.home)

    def test_refuse_credential_url_origin(self):
        git = self.fake_git

        def credential(source, *args):
            if args == ("remote", "get-url", "origin"):
                return b"https://user:secret@github.com/MichalMatu/local-agent-public.git"
            return git(source, *args)

        with mock.patch.object(plan, "_git", side_effect=credential):
            with self.assertRaisesRegex(plan.StagedLaunchdRejected, "absent or untrusted"):
                plan.verify_staged_source(home=self.home)

    def test_refuse_dirty_candidate(self):
        with mock.patch.object(plan, "_git", return_value=b"dirty"):
            with self.assertRaises(plan.StagedLaunchdRejected):
                plan.verify_staged_source(home=self.home)

    def test_refuse_digest_mismatch(self):
        (self.source / candidate.PATHS[0]).write_text("modified\n")
        with self.assertRaisesRegex(plan.StagedLaunchdRejected, "bytes do not match"):
            plan.verify_staged_source(home=self.home)

    def test_refuse_source_symlink(self):
        path = self.source / candidate.PATHS[0]
        path.unlink()
        path.symlink_to(self.home / "other")
        with self.assertRaisesRegex(plan.StagedLaunchdRejected, "unsafe"):
            plan.verify_staged_source(home=self.home)

    def test_refuse_wrong_source_revision(self):
        original = self.fake_git

        def changed_git(root, *args):
            if args == ("rev-parse", "HEAD"):
                return b"0" * 40
            return original(root, *args)

        with mock.patch.object(plan, "_git", side_effect=changed_git):
            with self.assertRaisesRegex(plan.StagedLaunchdRejected, "full-tested"):
                plan.verify_staged_source(home=self.home)

    def test_refuse_nonprivate_release_root(self):
        self.release.chmod(0o755)
        with self.assertRaisesRegex(plan.StagedLaunchdRejected, "owner-private"):
            plan.verify_staged_source(home=self.home)

    def test_refuse_unexpected_baseline_plist(self):
        with mock.patch.object(
            installed, "_validated_original",
            side_effect=installed.LaunchdCandidateRejected("modified original"),
        ):
            with self.assertRaisesRegex(plan.StagedLaunchdRejected, "B2 launch policy"):
                plan.render_disabled_staged_plan(
                    home=self.home, installed_root=self.base,
                    original_plist=self.original,
                )


if __name__ == "__main__":
    unittest.main()
