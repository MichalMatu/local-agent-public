"""Packaging contract: source-only/headless checks do not import PySide6."""
from __future__ import annotations

import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class DesktopMacBundleTests(unittest.TestCase):
    def test_build_script_bash_syntax_and_invalid_args(self):
        script = ROOT / "scripts" / "build_desktop_menu_macos.sh"
        result = subprocess.run(["bash", "-n", str(script)], capture_output=True,
                                text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_onedir_app_bundle_and_explicit_output_guards(self):
        script = (ROOT / "scripts" / "build_desktop_menu_macos.sh").read_text()
        spec = (ROOT / "platforms" / "macos" / "native" / "packaging" / "LocalAgentMenu.spec").read_text()
        self.assertIn("BUNDLE(", spec)
        self.assertIn("LSUIElement", spec)
        self.assertIn("name=\"Local Agent Menu.app\"", spec)
        self.assertIn("console=False", spec)
        self.assertIn("COLLECT(", spec)
        self.assertNotIn("onefile", spec.lower())
        self.assertIn('if [[ -e "$out" || -L "$out" ]]', script)
        self.assertIn("PYINSTALLER_CONFIG_DIR", script)
        self.assertIn("codesign --verify --deep --strict", script)
        self.assertNotIn("launchctl", script)
        self.assertNotIn("operator-control", script)
        self.assertNotIn("/Applications/", script)

    def test_spec_source_root_uses_pyinstaller_spec_directory(self):
        spec_path = ROOT / "platforms/macos/native/packaging/LocalAgentMenu.spec"
        spec_source = spec_path.read_text()
        self.assertIn("root = Path(SPECPATH).resolve().parents[3]", spec_source)
        # PyInstaller SPECPATH is the containing DIRECTORY, not the spec file.
        self.assertEqual(spec_path.parent.resolve().parents[3], ROOT)
        self.assertTrue((ROOT / "scripts/desktop_menu_entry.py").is_file())

    def test_gui_only_entrypoint(self):
        code = (ROOT / "scripts" / "desktop_menu_entry.py").read_text()
        self.assertIn("from local_agent.desktop.tray import main", code)
        self.assertNotIn("local_agent.daemon", code)
        self.assertNotIn("operator_control", code)


if __name__ == "__main__":
    unittest.main()
