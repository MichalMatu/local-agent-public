"""Fast, synthetic quality-gate failure tests; no network or production state."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from scripts.quality.check import (
    ACTIVE_DOCS,
    MODULE_ROOTS,
    SCOPED_GUIDANCE,
    check_agents,
    check_core_boundaries,
    check_docs,
    check_module_budget,
    local_link,
    markdown_destinations,
)


class QualityGateTests(unittest.TestCase):
    def test_live_document_links_and_scoped_guidance(self) -> None:
        root = Path(__file__).resolve().parents[1]
        errors, count = check_docs(root)
        self.assertEqual(errors, [])
        self.assertGreaterEqual(count, 30)
        self.assertEqual(check_agents(root), [])
        self.assertEqual(len(SCOPED_GUIDANCE), 9)
        self.assertGreaterEqual(len(ACTIVE_DOCS), 15)

    def test_doc_links_ignores_code_fences_and_external_links(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "README.md").write_text(
                "# Test\n[valid](page.md)\n[site](https://example.org)\n"
                "```md\n[absent](missing.md)\n```\n", encoding="utf-8",
            )
            (root / "page.md").write_text("# Actual", encoding="utf-8")
            errors, total = check_docs(root, ("README.md",))
            self.assertEqual(errors, [])
            self.assertEqual(total, 1)

    def test_doc_links_reports_missing_target_and_escape(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "repo"
            root.mkdir()
            (root / "README.md").write_text(
                "[missing](does-not-exist.md)\n"
                "[escape](../../other/secrets.md)\n", encoding="utf-8",
            )
            errors, count = check_docs(root, ("README.md",))
            self.assertEqual(count, 2)
            self.assertTrue(any("broken relative" in item for item in errors))
            self.assertTrue(any("escapes repository" in item for item in errors))

    def test_markdown_reference_links_are_checked(self) -> None:
        content = "See [details][ref].\n\n[ref]: local.md\n"
        self.assertEqual(markdown_destinations(content), ["local.md"])
        self.assertEqual(local_link("<file%20name.md>"), "file name.md")
        self.assertIsNone(local_link("#same-page"))
        self.assertIsNone(local_link("mailto:a@b.invalid"))

    def test_owners_are_required(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            errors = check_agents(Path(tmp))
            self.assertEqual(len(errors), 9)
            self.assertTrue(all("missing scoped" in item for item in errors))

    def test_pure_core_rejects_daemon_scheduler_and_gui_import(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for relative in (
                "platforms/macos/local-agent/runtime/health_snapshot.py",
                "platforms/macos/local-agent/runtime/backlog_snapshot.py",
                "platforms/macos/local-agent/repository/onboarding.py",
            ):
                file = root / relative
                file.parent.mkdir(parents=True, exist_ok=True)
                file.write_text("import os\n", encoding="utf-8")
            self.assertEqual(check_core_boundaries(root), [])
            (root / "platforms/macos/local-agent/repository/onboarding.py").write_text(
                "from local_agent.supervisor.worker import Worker\n"
                "from PySide6 import QtWidgets\n", encoding="utf-8",
            )
            issues = check_core_boundaries(root)
            self.assertEqual(len(issues), 2)
            self.assertTrue(any("PySide6" in item for item in issues))

    def test_oversized_new_module_and_growing_baseline_fail(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            folder = root / "platforms/macos/local-agent"
            folder.mkdir(parents=True)
            large = folder / "new.py"
            large.write_text("x = 1\n" * 11, encoding="utf-8")
            problems, found = check_module_budget(root, limit=10, baseline={}, roots=("platforms/macos/local-agent",))
            self.assertEqual(found, [("platforms/macos/local-agent/new.py", 11)])
            self.assertTrue(any("new oversized" in item for item in problems))
            problems, _ = check_module_budget(
                root, limit=10, baseline={"platforms/macos/local-agent/new.py": 10},
                roots=("platforms/macos/local-agent",),
            )
            self.assertTrue(any("complexity debt grew" in item for item in problems))
            problems, _ = check_module_budget(
                root, limit=10, baseline={"platforms/macos/local-agent/new.py": 11},
                roots=("platforms/macos/local-agent",),
            )
            self.assertEqual(problems, [])

    def test_every_runtime_platform_is_in_budget(self) -> None:
        self.assertEqual(len(MODULE_ROOTS), 5)
        root = Path(__file__).resolve().parents[1]
        problems, _ = check_module_budget(root)
        self.assertEqual(problems, [])

    def test_other_platform_budget_and_test_exclusion(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for relative in MODULE_ROOTS:
                folder = root / relative
                folder.mkdir(parents=True)
                (folder / "safe.py").write_text("pass\n", encoding="utf-8")
            linux = root / "platforms/linux/core/linux_agent"
            linux.mkdir()
            (linux / "unsafe.py").write_text("pass\n" * 11, encoding="utf-8")
            tests = linux / "tests"
            tests.mkdir()
            (tests / "fixture.py").write_text("pass\n" * 20, encoding="utf-8")
            problems, oversized = check_module_budget(root, limit=10, baseline={})
            self.assertEqual(oversized, [
                ("platforms/linux/core/linux_agent/unsafe.py", 11),
            ])
            self.assertTrue(any("new oversized module" in item for item in problems))

    def test_missing_platform_budget_root_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            problems, _ = check_module_budget(Path(tmp), baseline={})
            self.assertEqual(len(problems), len(MODULE_ROOTS))
            self.assertTrue(all("package missing" in issue for issue in problems))

    def test_stale_baseline_detected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "platforms/macos/local-agent").mkdir(parents=True)
            problems, _ = check_module_budget(
                root, limit=10, baseline={"platforms/macos/local-agent/gone.py": 13},
                roots=("platforms/macos/local-agent",),
            )
            self.assertTrue(any("stale oversized baseline" in i for i in problems))


if __name__ == "__main__":
    unittest.main()
