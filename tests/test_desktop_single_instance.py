"""Optional GUI-only cross-process lock and private-directory contract."""
from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from local_agent.desktop.single_instance import acquire_desktop_menu_lock


class DesktopMenuLockTests(unittest.TestCase):
    def test_import_never_imports_qt_or_daemon(self):
        check = subprocess.run(
            [sys.executable, "-c",
             "import sys; import local_agent.desktop.single_instance;"
             "assert 'PySide6' not in sys.modules;"
             "assert 'local_agent.daemon.service' not in sys.modules"],
            capture_output=True, text=True, timeout=10,
        )
        self.assertEqual(check.returncode, 0, check.stderr)

    def test_invalid_relative_directory_rejected_before_qt(self):
        if importlib.util.find_spec("PySide6") is None:
            self.skipTest("optional PySide6 absent")
        with self.assertRaises(ValueError):
            acquire_desktop_menu_lock(directory=Path("relative-lock"))

    @unittest.skipUnless(importlib.util.find_spec("PySide6"), "optional PySide6 absent")
    def test_two_processes_are_serialized_and_released(self):
        # The test uses its own temporary lock directory, never the GUI's
        # actual cache path or installed daemon runtime.
        with tempfile.TemporaryDirectory() as root:
            directory = Path(root) / "gui-only-lock"
            first = acquire_desktop_menu_lock(directory=directory)
            self.assertIsNotNone(first)
            self.assertTrue(first.isLocked())
            self.assertEqual(first.staleLockTime(), 0)
            script = (
                "from pathlib import Path;"
                "from local_agent.desktop.single_instance import acquire_desktop_menu_lock;"
                "import sys;"
                "lock=acquire_desktop_menu_lock(directory=Path(sys.argv[1]));"
                "assert lock is None, 'second GUI acquired an occupied lock';"
                "print('SECOND_GUI_REFUSED')"
            )
            second = subprocess.run(
                [sys.executable, "-c", script, str(directory)],
                capture_output=True, text=True, timeout=15,
            )
            self.assertEqual(second.returncode, 0, second.stderr)
            self.assertIn("SECOND_GUI_REFUSED", second.stdout)
            first.unlock()
            next_instance = acquire_desktop_menu_lock(directory=directory)
            self.assertIsNotNone(next_instance)
            next_instance.unlock()

    @unittest.skipUnless(importlib.util.find_spec("PySide6"), "optional PySide6 absent")
    def test_symlink_or_insecure_directory_denied(self):
        with tempfile.TemporaryDirectory() as root:
            tmp = Path(root)
            original = tmp / "real"
            original.mkdir()
            link = tmp / "shortcut"
            link.symlink_to(original, target_is_directory=True)
            with self.assertRaises(RuntimeError):
                acquire_desktop_menu_lock(directory=link)
            if os.name == "posix":
                original.chmod(0o777)
                with self.assertRaises(RuntimeError):
                    acquire_desktop_menu_lock(directory=original)
                original.chmod(0o700)


if __name__ == "__main__":
    unittest.main()
