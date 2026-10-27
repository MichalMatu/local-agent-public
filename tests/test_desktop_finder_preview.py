"""Operator-only Finder preview preparation and guarded cleanup contract."""
from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "prepare_desktop_menu_finder_macos.sh"
GUI_SHA = "9bfd102c0d663304acba52272a9be04e9e39fc4b"
RUNBOOK = ROOT / "docs" / "DESKTOP_MENU_BUNDLE.md"


class FinderPreviewTests(unittest.TestCase):
    def test_bash_syntax(self):
        result = subprocess.run(["bash", "-n", str(SCRIPT)],
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_finder_runbook_preserves_frozen_prototype_boundary(self):
        runbook = RUNBOOK.read_text("utf8")
        self.assertIn("frozen historical prototype", runbook)
        self.assertIn("not** the installed Local Agent service or a customer installer", runbook)
        self.assertIn("platforms/macos/installer/INSTALL_WITH_CODEX.md", runbook)
        self.assertIn("original historical procedure", runbook)
        self.assertNotIn("```bash", runbook)

    def test_only_operator_modes_exact_sha_and_no_automatic_launch(self):
        text = SCRIPT.read_text("utf8")
        for expected in (GUI_SHA, "--check|--prepare", "--cleanup",
                         '[[ ! -t 0 || ! -t 1 ]]',
                         'git -C "$root/source" rev-parse HEAD',
                         'open -R "$app"', 'FINDER_PREVIEW_READY',
                         'cp -- "$0" "$root/cleanup-preview.sh"',
                         "trap cleanup_on_failure EXIT"):
            self.assertIn(expected, text)
        for denied in ('open "$app"', "launchctl", "/Applications/",
                       "operator-control.sha", "spctl --master-disable"):
            self.assertNotIn(denied, text)

    def test_cleanup_strict_path_and_marker(self):
        text = SCRIPT.read_text("utf8")
        for expected in ('^/private/tmp/local-agent-menu-finder-[A-Za-z0-9]{8}$',
                         '[[ -d "$dir" && ! -L "$dir" ]]',
                         'stat -f %u "$dir"', 'stat -f %Lp "$dir"',
                         '! -L "$dir/$MARKER"', 'safe_preview_dir "$2"',
                         'rm -rf -- "$2"', "prepared=1",
                         'chmod 700 "$root/cleanup-preview.sh"'):
            self.assertIn(expected, text)

    def test_rejects_invalid_cli_before_any_effect(self):
        for args in ([], ["--bad"], ["--prepare", "extra"],
                     ["--check", "extra"], ["--cleanup"]):
            with self.subTest(args=args):
                r = subprocess.run(["bash", str(SCRIPT), *args],
                                   capture_output=True, text=True, timeout=10)
                self.assertEqual(r.returncode, 2, r.stderr)
                self.assertIn("Usage:", r.stderr)

    @unittest.skipUnless(sys.platform == "darwin", "macOS cleanup only")
    def test_cleanup_does_not_accept_arbitrary_paths(self):
        for path in ("/", "/private/tmp", "/private/tmp/something",
                     "/private/tmp/local-agent-menu-finder-ABCDEFGH/.."):
            with self.subTest(path=path):
                r = subprocess.run(["bash", str(SCRIPT), "--cleanup", path],
                                   capture_output=True, text=True, timeout=10)
                self.assertEqual(r.returncode, 3, r.stderr)
                self.assertIn("Refusing cleanup", r.stderr)


if __name__ == "__main__":
    unittest.main()
