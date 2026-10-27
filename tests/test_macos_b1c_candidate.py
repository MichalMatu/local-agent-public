"""Isolated Mac B2-to-B1c overlay staging integrity regressions."""
from __future__ import annotations

import json
from pathlib import Path
import plistlib
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from scripts import macos_b1c_candidate as candidate
from scripts import macos_b1c_launchd_plan as launchd_plan
from local_agent.platform import macos_launchd


@unittest.skipUnless(sys.platform == "darwin", "Mac-only B1c candidate staging")
class MacB1cCandidateTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="la-rollout-b1c-")
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve(strict=True)
        self.root.chmod(0o700)
        self.marker = self.root / candidate.MARKER
        self.marker.write_bytes(candidate.MARKER_BYTES)
        self.marker.chmod(0o600)
        self.checkout = self.root / "candidate"
        self.checkout.mkdir(mode=0o700)
        self.git("init", "-q")
        self.git("config", "user.name", "Local Agent disposable test")
        self.git("config", "user.email", "local-agent-tests@example.invalid")
        for path in candidate.MODIFIED:
            target = self.checkout / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(f"original {path}\n")
        (self.checkout / "agent_entrypoint.py").write_text("# sealed unchanged B2 entrypoint\\n")
        self.git("add", ".")
        self.git("commit", "-q", "-m", "Disposable B2 baseline")
        self.base = self.git("rev-parse", "HEAD").strip()
        for path in candidate.PATHS:
            target = self.checkout / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(f"candidate {path}\n")
        self.git("add", ".")
        self.git("commit", "-q", "-m", "Disposable B1c overlay")
        self.source = self.git("rev-parse", "HEAD").strip()
        self.git("checkout", "-q", "--detach", self.base)
        self.patch_base = mock.patch.object(candidate, "BASE_SHA", self.base)
        self.patch_base.start()
        self.addCleanup(self.patch_base.stop)

    def git(self, *args):
        result = subprocess.run(
            ["git", "-C", str(self.checkout), *args],
            capture_output=True, text=True, check=False, timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout

    def test_actual_git_overlay_stages_only_pinned_files(self):
        report = candidate.stage(self.root, self.source)
        self.assertEqual(report["base_sha"], self.base)
        self.assertEqual(report["overlay_sha"], self.source)
        self.assertEqual(set(report["files"]), set(candidate.PATHS))
        self.assertEqual(report["changed_source_files"], 8)
        self.assertEqual(report["added_source_files"], 3)
        self.assertFalse(report["installed_service_changed"])
        self.assertFalse(report["engine_started"])
        self.assertEqual(candidate.verify(self.root, self.source), report)
        for path in candidate.PATHS:
            self.assertTrue((self.checkout / path).is_file())

    def test_sealed_candidate_is_clean_distinct_revision_with_B2_parent(self):
        candidate.stage(self.root, self.source)
        sealed = candidate.seal(self.root, self.source)
        self.assertNotEqual(sealed["candidate_sha"], self.base)
        self.assertNotEqual(sealed["candidate_sha"], self.source)
        self.assertTrue(sealed["clean_git_checkout"])
        self.assertEqual(self.git("rev-parse", "HEAD^").strip(), self.base)
        self.assertEqual(self.git("status", "--porcelain"), "")
        self.assertEqual(sealed, candidate.verify_sealed(self.root, self.source))
        with self.assertRaisesRegex(candidate.CandidateRejected, "not the installed"):
            candidate.stage(self.root, self.source)

    def test_modified_sealed_candidate_is_not_accepted(self):
        candidate.stage(self.root, self.source)
        candidate.seal(self.root, self.source)
        (self.checkout / candidate.MODIFIED[0]).write_text("tampered candidate\n")
        with self.assertRaisesRegex(candidate.CandidateRejected, "dirty"):
            candidate.verify_sealed(self.root, self.source)

    def test_damaged_source_after_staging_is_rejected(self):
        candidate.stage(self.root, self.source)
        target = self.checkout / candidate.MODIFIED[0]
        target.write_text("corrupted\n")
        with self.assertRaisesRegex(candidate.CandidateRejected, "digest mismatch"):
            candidate.verify(self.root, self.source)

    def test_dirty_baseline_and_marker_attack_rejected_before_restore(self):
        (self.checkout / candidate.MODIFIED[0]).write_text("user content\n")
        with self.assertRaisesRegex(candidate.CandidateRejected, "not clean"):
            candidate.stage(self.root, self.source)
        self.git("checkout", "--", candidate.MODIFIED[0])
        self.marker.chmod(0o644)
        with self.assertRaisesRegex(candidate.CandidateRejected, "marker"):
            candidate.stage(self.root, self.source)

    def test_wrong_revision_or_unrelated_payload_rejected(self):
        with self.assertRaisesRegex(candidate.CandidateRejected, "exact SHA"):
            candidate.stage(self.root, "main")
        with self.assertRaisesRegex(candidate.CandidateRejected, "Git verification"):
            candidate.stage(self.root, "a" * 40)
        candidate.stage(self.root, self.source)
        (self.checkout / "unexpected.json").write_text(json.dumps({"x": 1}))
        with self.assertRaisesRegex(candidate.CandidateRejected, "unexpected changed"):
            candidate.verify(self.root, self.source)

    def test_live_launchd_installed_source_requires_exact_B2_git(self):
        live = self.root / "fake-installed"
        live.mkdir(mode=0o700)
        home = self.root
        with self.assertRaisesRegex(
            launchd_plan.LaunchdCandidateRejected, "not the accepted B2"
        ):
            launchd_plan._verify_accepted_installed_base(live, home)

    def launchd_inputs(self):
        # Unit fixture has a synthetic B2 parent; real live source validation
        # runs separately on the actual immutable source-cccc67fc checkout.
        self.enterContext(mock.patch.object(
            launchd_plan, "_verify_accepted_installed_base", return_value=None,
        ))
        installed = self.root / "original-installed"
        home = self.root / "original-home"
        installed.mkdir(mode=0o700)
        home.mkdir(mode=0o700)
        original = plistlib.dumps(macos_launchd.build_launch_agent(
            "parallel", repo_root=installed, home=home, max_workers=2,
        ))
        return installed, home, original

    def test_launchd_candidate_plan_is_disabled_and_requires_admission(self):
        from local_agent.supervisor.macos_release_admission import REQUIRED_ENV

        candidate.stage(self.root, self.source)
        candidate.seal(self.root, self.source)
        installed, home, original = self.launchd_inputs()
        with self.assertRaisesRegex(
            launchd_plan.LaunchdCandidateRejected, "fully tested release SHA"
        ):
            launchd_plan.render_disabled_candidate_plan(
                sandbox=self.root, overlay_sha=self.source,
                original_plist=original, installed_root=installed, home=home,
            )
        self.enterContext(mock.patch.object(
            launchd_plan, "APPROVED_TESTED_CANDIDATE_SHA",
            self.git("rev-parse", "HEAD").strip(),
        ))
        plan = launchd_plan.render_disabled_candidate_plan(
            sandbox=self.root, overlay_sha=self.source, original_plist=original,
            installed_root=installed, home=home,
        )
        document = plistlib.loads(plan)
        baseline = plistlib.loads(original)
        self.assertEqual(document["Label"], baseline["Label"])
        self.assertTrue(document["Disabled"])
        self.assertTrue(document["KeepAlive"])
        self.assertEqual(
            document["ProgramArguments"][1],
            str(self.checkout / "agent_entrypoint.py"),
        )
        self.assertEqual(
            document["ProgramArguments"][0], baseline["ProgramArguments"][0]
        )
        self.assertEqual(
            document["EnvironmentVariables"][REQUIRED_ENV], "1"
        )
        self.assertNotIn(REQUIRED_ENV, baseline["EnvironmentVariables"])
        self.assertEqual(
            launchd_plan.inspect_disabled_candidate_plan(
                plan, sandbox=self.root, overlay_sha=self.source,
                original_plist=original, installed_root=installed, home=home,
            )["installed"], False
        )
        self.assertFalse((home / "Library" / "LaunchAgents").exists())

    def test_launchd_plan_rejects_modified_policy_and_source(self):
        candidate.stage(self.root, self.source)
        candidate.seal(self.root, self.source)
        installed, home, original = self.launchd_inputs()
        with self.assertRaisesRegex(
            launchd_plan.LaunchdCandidateRejected, "fully tested release SHA"
        ):
            launchd_plan.render_disabled_candidate_plan(
                sandbox=self.root, overlay_sha=self.source,
                original_plist=original, installed_root=installed, home=home,
            )
        self.enterContext(mock.patch.object(
            launchd_plan, "APPROVED_TESTED_CANDIDATE_SHA",
            self.git("rev-parse", "HEAD").strip(),
        ))
        fixture = plistlib.loads(original)
        fixture["KeepAlive"] = False
        with self.assertRaisesRegex(
            launchd_plan.LaunchdCandidateRejected, "trusted B2 policy"
        ):
            launchd_plan.render_disabled_candidate_plan(
                sandbox=self.root, overlay_sha=self.source,
                original_plist=plistlib.dumps(fixture),
                installed_root=installed, home=home,
            )
        valid = launchd_plan.render_disabled_candidate_plan(
            sandbox=self.root, overlay_sha=self.source, original_plist=original,
            installed_root=installed, home=home,
        )
        tampered = plistlib.loads(valid)
        del tampered["Disabled"]
        with self.assertRaisesRegex(
            launchd_plan.LaunchdCandidateRejected, "not canonical"
        ):
            launchd_plan.inspect_disabled_candidate_plan(
                plistlib.dumps(tampered), sandbox=self.root,
                overlay_sha=self.source, original_plist=original,
                installed_root=installed, home=home,
            )
        (self.checkout / candidate.MODIFIED[0]).write_text("corrupted\\n")
        with self.assertRaisesRegex(
            launchd_plan.LaunchdCandidateRejected, "not exact sealed"
        ):
            launchd_plan.render_disabled_candidate_plan(
                sandbox=self.root, overlay_sha=self.source,
                original_plist=original, installed_root=installed, home=home,
            )

    def test_cleanup_or_service_mutation_is_not_exposed(self):
        self.assertFalse(hasattr(candidate, "install"))
        self.assertFalse(hasattr(candidate, "activate"))
        self.assertFalse(hasattr(candidate, "delete"))
        self.assertFalse(hasattr(candidate, "unfreeze"))
        self.assertFalse(hasattr(candidate, "restart"))


if __name__ == "__main__":
    unittest.main()
