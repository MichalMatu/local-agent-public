"""Safety contract for the operator-only, disposable macOS menu preview."""
from __future__ import annotations

import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PREVIEW = ROOT / "scripts" / "preview_desktop_menu_macos.sh"
PINNED = "84255bc868730ffc25da07189a59245095a77446"


class MacPreviewScriptTests(unittest.TestCase):
    def test_bash_syntax(self):
        result = subprocess.run(
            ["bash", "-n", str(PREVIEW)],
            capture_output=True, text=True, timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_exact_source_and_only_opt_in_modes(self):
        content = PREVIEW.read_text(encoding="utf-8")
        self.assertIn(PINNED, content)
        self.assertIn('if [[ "$#" -ne 1 ]]; then usage; fi', content)
        self.assertIn("--check|--preview", content)
        self.assertIn('[[ ! -t 0 ]]', content)
        self.assertIn('[[ ! -t 1 ]]', content)
        self.assertIn('git -C "$root/source" rev-parse HEAD', content)
        self.assertIn('trap cleanup EXIT', content)

    def test_isolated_qt_and_user_session_not_daemon(self):
        content = PREVIEW.read_text(encoding="utf-8")
        self.assertIn('"$source_root/.venv/bin/python" -m venv "$root/qt-env"', content)
        self.assertIn('"$root/qt-env/bin/python" -m pip install', content)
        self.assertIn('requirements-desktop.txt', content)
        self.assertIn('unset QT_QPA_PLATFORM', content)
        self.assertIn('"$root/qt-env/bin/python" -m local_agent.desktop.tray', content)
        self.assertNotIn("launchctl bootstrap", content)
        self.assertNotIn("launchctl kickstart", content)
        self.assertNotIn("systemctl", content)
        self.assertNotIn("operator-control.sha", content)
        self.assertNotIn("--install", content)

    def test_rejects_bad_cli_before_side_effects(self):
        for argv in ([], ["--dangerous"], ["--check", "--preview"]):
            with self.subTest(argv=argv):
                result = subprocess.run(
                    ["bash", str(PREVIEW), *argv],
                    capture_output=True, text=True, timeout=10,
                )
                self.assertEqual(result.returncode, 2)
                self.assertIn("Usage:", result.stderr)


if __name__ == "__main__":
    unittest.main()
