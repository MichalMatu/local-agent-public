"""Offline, temporary-Git tests for the non-inventing session handoff protocol."""
from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from scripts import handoff


class HandoffGeneratorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.workspace = tempfile.TemporaryDirectory(prefix="la-handoff-unit-")
        self.addCleanup(self.workspace.cleanup)
        self.base = Path(self.workspace.name).resolve()
        self.root = self.base / "source"
        self.root.mkdir()
        self._git("init", "--quiet", "-b", handoff.DEVELOPMENT_BRANCH)
        self._git("config", "user.name", "Handoff Fixture")
        self._git("config", "user.email", "handoff@example.invalid")
        for name in handoff.CANONICAL_DOCS:
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            text = ("# " + name + "\n"
                    + "\n".join(handoff.CONTRACT_TEXT.get(name, ()))
                    + f"\n{handoff.DEVELOPMENT_BRANCH}\n")
            path.write_text(text, encoding="utf-8")
        self._git("add", ".")
        self._git("commit", "--quiet", "-m", "fixture: docs")

    def _git(self, *args: str) -> str:
        return subprocess.check_output(
            ["git", "-C", str(self.root), *args], text=True,
            stderr=subprocess.DEVNULL,
        ).strip()

    def test_clean_exact_source_produces_handoff_without_live_claims(self) -> None:
        result = handoff.snapshot(self.root)
        self.assertEqual(result["generation_status"], "pass")
        self.assertTrue(result["source"]["clean"])
        self.assertEqual(result["source"]["sha"], self._git("rev-parse", "HEAD"))
        text = handoff.markdown(self.root, result)
        self.assertIn("NOT CHECKED", text)
        self.assertIn("NOT_PROVIDED", text)
        self.assertIn("INSTALL_WITH_CODEX.md", text)
        self.assertIn("Do next", text)
        self.assertIn("Follow the owner's assigned scope", text)
        self.assertNotIn("Begin from the prioritized pre-public P0/P1/P2 audit", text)
        self.assertNotIn("Pi state and operator pins/grants: **NOT CHECKED**", text)
        self.assertNotIn("never re-arm release watchers, merge, restart, deploy, touch Pi/GUI", text)
        self.assertNotIn("Continue the CLI doctor/menu", text)
        self.assertIn(self._git("rev-parse", "HEAD"), text)
        self.assertNotIn("Live Mac daemon and claims/leases: **PASS**", text)
        self.assertFalse(result["source"]["remote_identity_verified"])
        self.assertEqual(result["release_acceptance"], "NOT_ASSESSED_OFFLINE")
        self.assertNotIn("release_status", result)

    def test_dirty_worktree_is_blocking_not_silently_clean(self) -> None:
        (self.root / "dirty.txt").write_text("untracked", encoding="utf-8")
        result = handoff.snapshot(self.root)
        self.assertEqual(result["generation_status"], "fail")
        self.assertTrue(any("uncommitted" in e for e in result["contract_errors"]))

    def test_missing_document_blocks_handoff(self) -> None:
        (self.root / "docs" / "QUALITY_TOOLING.md").unlink()
        result = handoff.snapshot(self.root)
        self.assertEqual(result["generation_status"], "fail")
        self.assertTrue(any("document missing" in e for e in result["contract_errors"]))

    def test_required_decisions_cannot_disappear(self) -> None:
        path = self.root / "AGENTS.md"
        path.write_text("# empty\n", encoding="utf-8")
        failures = handoff.audit_docs(self.root, handoff.source_state(self.root))
        self.assertTrue(any("contract missing" in e for e in failures))

    def test_canonical_main_branch_is_supported(self) -> None:
        if self._git("branch", "--show-current") != "main":
            self._git("checkout", "--quiet", "-b", "main")
        report = handoff.snapshot(self.root)
        self.assertEqual(report["generation_status"], "pass")
        self.assertEqual(report["source"]["branch"], "main")

    def test_wrong_branch_fails_closed(self) -> None:
        self._git("checkout", "--quiet", "-b", "experimental-unrelated")
        issues = handoff.audit_docs(self.root, handoff.source_state(self.root))
        self.assertTrue(any("branch differs" in i for i in issues))

    def test_review_branch_passes_source_guard_not_strict_handoff(self) -> None:
        self._git("checkout", "--quiet", "-b", "audit/source-quality")
        strict = handoff.snapshot(self.root)
        source = handoff.snapshot(self.root, require_approved_branch=False)
        self.assertEqual(strict["generation_status"], "fail")
        self.assertTrue(any("branch differs" in issue for issue in strict["contract_errors"]))
        self.assertEqual(source["generation_status"], "pass")
        self.assertTrue(source["source"]["clean"])

    def test_detached_exact_sha_is_supported_for_native_testing(self) -> None:
        sha = self._git("rev-parse", "HEAD")
        self._git("checkout", "--quiet", "--detach", sha)
        report = handoff.snapshot(self.root)
        self.assertTrue(report["source"]["detached"])
        self.assertIsNone(report["source"]["branch"])
        self.assertEqual(report["generation_status"], "pass")

    def test_stale_evidence_never_relabels_old_pass_as_current(self) -> None:
        receipt = self.base / "receipt.json"
        receipt.write_text(json.dumps({
            "version": 1,
            "source_sha": "0" * 40,
            "checks": [{"name": "full native smoke", "status": "PASS"}],
        }), encoding="utf-8")
        result = handoff.snapshot(self.root, receipt)
        self.assertEqual(result["test_evidence"]["status"], "STALE_SHA_UNVERIFIED")
        self.assertEqual(result["test_evidence"]["checks"], [])
        self.assertNotIn("full native smoke", handoff.markdown(self.root, result))

    def test_matching_evidence_is_labelled_operator_supplied_only(self) -> None:
        receipt = self.base / "receipt.json"
        receipt.write_text(json.dumps({
            "version": 1,
            "source_sha": self._git("rev-parse", "HEAD"),
            "checks": [{"name": "quick native", "status": "PASS"},
                       {"name": "complete native", "status": "TIMEOUT"}],
        }), encoding="utf-8")
        result = handoff.snapshot(self.root, receipt)
        text = handoff.markdown(self.root, result)
        self.assertIn("quick native: **PASS**", text)
        self.assertIn("complete native: **TIMEOUT**", text)
        self.assertIn("not independently verified by generator", text)

    def test_receipt_rejects_unbounded_or_untrusted_names(self) -> None:
        receipt = self.base / "receipt.json"
        receipt.write_text(json.dumps({
            "version": 1, "source_sha": self._git("rev-parse", "HEAD"),
            "checks": [{"name": "<script>secret</script>", "status": "PASS"}],
        }), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "invalid handoff evidence"):
            handoff.snapshot(self.root, receipt)

    def test_output_is_private_exclusive_and_never_written_into_repo(self) -> None:
        content = handoff.markdown(self.root, handoff.snapshot(self.root))
        output = self.base / "handoff.md"
        handoff._write_private_output(output, self.root, content)
        self.assertTrue(output.is_file())
        self.assertEqual(output.read_text(encoding="utf-8"), content)
        self.assertEqual(output.stat().st_mode & 0o777, 0o600)
        with self.assertRaises(FileExistsError):
            handoff._write_private_output(output, self.root, content)
        with self.assertRaisesRegex(ValueError, "outside source"):
            handoff._write_private_output(
                self.root / "handoff.md", self.root, content,
            )

    def test_never_query_remote_or_mutate_the_git_checkout(self) -> None:
        old_sha = self._git("rev-parse", "HEAD")
        result = handoff.snapshot(self.root)
        self.assertEqual(result["generation_status"], "pass")
        self.assertEqual(self._git("rev-parse", "HEAD"), old_sha)
        self.assertEqual(self._git("status", "--porcelain"), "")


if __name__ == "__main__":
    unittest.main()
