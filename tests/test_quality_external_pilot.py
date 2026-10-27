"""Fail-closed external tooling gate; no third-party package needed for these tests."""
from __future__ import annotations

import importlib.metadata
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scripts.quality import external_pilot


class ExternalPilotTests(unittest.TestCase):
    def test_missing_tool_is_unverified_instead_of_green(self) -> None:
        with tempfile.TemporaryDirectory(prefix="la-external-stub-") as tmp:
            root = Path(tmp).resolve()
            (root / "platforms/macos/local-agent").mkdir(parents=True)
            with mock.patch.object(external_pilot, "verify_environment",
                                   return_value=["ty:missing"]), \
                 mock.patch.object(external_pilot.subprocess, "run") as spawn:
                report = external_pilot.run(root)
            self.assertEqual(report["status"], "unverified")
            spawn.assert_not_called()

    def test_production_interpreter_is_not_an_approved_pilot(self) -> None:
        with mock.patch.object(external_pilot.sys, "prefix", "/usr"), \
             mock.patch.object(external_pilot.sys, "base_prefix", "/usr"), \
             mock.patch.object(importlib.metadata, "version",
                               side_effect=importlib.metadata.PackageNotFoundError):
            issues = external_pilot.verify_environment()
        self.assertIn("disposable_virtualenv_required", issues)
        self.assertTrue(any("missing" in issue for issue in issues))

    def test_three_real_commands_called_only_after_versions_verified(self) -> None:
        with tempfile.TemporaryDirectory(prefix="la-external-stub-") as tmp:
            root = Path(tmp).resolve()
            (root / "platforms/macos/local-agent").mkdir(parents=True)
            ok = mock.Mock(returncode=0, stdout="verified\n", stderr="")
            with mock.patch.object(external_pilot, "verify_environment", return_value=[]), \
                 mock.patch.object(external_pilot.subprocess, "run", return_value=ok) as spawn:
                result = external_pilot.run(root)
            self.assertEqual(result["status"], "pass")
            self.assertEqual([s["name"] for s in result["stages"]],
                             ["imports", "types", "properties"])
            self.assertEqual(spawn.call_count, 3)
            self.assertTrue(all(call.kwargs["timeout"] == 150
                                for call in spawn.call_args_list))
            self.assertNotEqual(spawn.call_args_list[0].kwargs["cwd"], root)
            self.assertNotEqual(spawn.call_args_list[1].kwargs["cwd"], root)
            self.assertTrue(all(call.kwargs["cwd"] == root
                                for call in spawn.call_args_list[2:]))

    def test_import_view_exposes_actual_package_and_is_removed_after_tool(self) -> None:
        root = Path(__file__).resolve().parents[1]
        views = []

        def inspect(command, **kwargs):
            view = kwargs["cwd"]
            views.append(view)
            self.assertEqual((view / "local_agent").resolve(),
                             root / "platforms/macos/local-agent")
            self.assertFalse((view / "operator").exists())
            return mock.Mock(returncode=0)

        with mock.patch.object(external_pilot.subprocess, "run", side_effect=inspect):
            external_pilot._run_command(["lint-imports"], root, import_view=True)
        self.assertFalse(views[0].exists())

    def test_validation_failure_does_not_run_later_tools_or_leak_stderr(self) -> None:
        with tempfile.TemporaryDirectory(prefix="la-external-stub-") as tmp:
            root = Path(tmp).resolve()
            (root / "platforms/macos/local-agent").mkdir(parents=True)
            failed = mock.Mock(returncode=1, stdout="", stderr="FAKE_SECRET_IN_CODE")
            with mock.patch.object(external_pilot, "verify_environment", return_value=[]), \
                 mock.patch.object(external_pilot.subprocess, "run",
                                   return_value=failed) as spawn:
                result = external_pilot.run(root)
            self.assertEqual(result["status"], "fail")
            self.assertEqual(spawn.call_count, 1)
            self.assertEqual(result["stages"][0]["name"], "imports")
            self.assertNotIn("FAKE_SECRET_IN_CODE", repr(result))

    def test_crashed_tool_is_unverified(self) -> None:
        with tempfile.TemporaryDirectory(prefix="la-external-stub-") as tmp:
            root = Path(tmp).resolve()
            (root / "platforms/macos/local-agent").mkdir(parents=True)
            with mock.patch.object(external_pilot, "verify_environment", return_value=[]), \
                 mock.patch.object(external_pilot.subprocess, "run",
                                   side_effect=OSError("synthetic")):
                result = external_pilot.run(root)
            self.assertEqual(result["status"], "unverified")


if __name__ == "__main__":
    unittest.main()
