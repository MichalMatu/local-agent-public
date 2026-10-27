"""Preserve installer public import identities after source-only move."""
from __future__ import annotations

import importlib
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MOVED = (
    "macos_launchd",
    "macos_launchd_probe",
    "macos_launchd_recovery_plan",
    "macos_b1c_candidate",
    "macos_b1c_launchd_plan",
    "macos_b1c_staged_launchd_plan",
    "macos_b1c_disposable_service_recovery",
    "macos_installed_state_preflight",
    "macos_chat_bridge",
    "macos_release_fence_rehearsal",
    "macos_release_quiescence_rehearsal",
)


class InstallerSourceLayoutTests(unittest.TestCase):
    def test_legacy_imports_reference_relocated_single_implementation(self) -> None:
        for name in MOVED:
            with self.subTest(name=name):
                old = importlib.import_module("scripts." + name)
                new = importlib.import_module("platforms.macos.installer." + name)
                self.assertIs(old, new)
                self.assertEqual(Path(new.__file__).resolve(),
                                 ROOT / "platforms/macos/installer" / (name + ".py"))

    def test_old_command_names_remain_valid_from_other_cwd(self) -> None:
        with tempfile.TemporaryDirectory(prefix="macos installer cmd ") as d:
            for name in ("macos_launchd", "macos_b1c_candidate", "macos_chat_bridge"):
                with self.subTest(name=name):
                    cmd = [sys.executable, str(ROOT / "scripts" / (name + ".py")), "--help"]
                    proc = subprocess.run(cmd, cwd=d, capture_output=True, text=True,
                                          timeout=15, check=False)
                    self.assertEqual(proc.returncode, 0, proc.stderr)
                    self.assertIn("usage:", proc.stdout)

    def test_critical_standalone_updater_is_not_import_shim(self) -> None:
        for name in ("macos_host_self_update", "macos_host_service",
                     "macos_cli_polling_rollout", "macos_rollout_boot_recovery",
                     "macos_repository_maintenance"):
            with self.subTest(name=name):
                source = (ROOT / "scripts" / (name + ".py")).read_text()
                self.assertNotIn("platforms.macos.installer.", source)


if __name__ == "__main__":
    unittest.main()
