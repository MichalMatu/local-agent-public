"""Offline, disposable Git repository tests for bounded Debian Host Ops."""
from __future__ import annotations

import pathlib
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from linux_agent.host_ops_git import GitInspectionError, inspect_git


def run(repo: pathlib.Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=10)


class GitInspectTests(unittest.TestCase):
    def test_real_git_clean_dirty_and_detached(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            run(root, "init", "-q")
            run(root, "config", "user.name", "HostOps Test")
            run(root, "config", "user.email", "test@example.invalid")
            (root / "data").write_text("test\n")
            run(root, "add", "data")
            run(root, "commit", "-qm", "baseline")
            clean = inspect_git(str(root))
            self.assertFalse(clean["dirty"])
            self.assertEqual(len(clean["head"]), 40)
            self.assertEqual(clean["root"], str(root))
            (root / "untracked").write_text("not checked in")
            dirty = inspect_git(str(root))
            self.assertTrue(dirty["dirty"])
            self.assertGreater(dirty["status_entry_count"], 0)
            self.assertNotIn("untracked", str(dirty))
            (root / "untracked").unlink()
            run(root, "checkout", "--detach", "-q")
            self.assertIsNone(inspect_git(str(root))["branch"])

    def test_invalid_target_and_unavailable_git(self):
        with tempfile.TemporaryDirectory() as tmp:
            for bad in ("relative", str(pathlib.Path(tmp) / ".." / "a"), str(pathlib.Path(tmp) / "missing")):
                with self.assertRaises(GitInspectionError):
                    inspect_git(bad)
            for bad_timeout in (0, -1, 61):
                with self.assertRaises(GitInspectionError):
                    inspect_git(tmp, timeout=bad_timeout)
            with self.assertRaises(GitInspectionError):
                inspect_git(tmp, max_output=1)
            with mock.patch("linux_agent.host_ops_git.shutil.which", return_value=None):
                with self.assertRaisesRegex(GitInspectionError, "unavailable"):
                    inspect_git(tmp)

    def test_nonrepository_and_operation_deadline(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(GitInspectionError):
                inspect_git(tmp)
            from linux_agent import host_ops_git
            with mock.patch.object(host_ops_git.time, "monotonic", side_effect=[0.0, 20.0]):
                with self.assertRaises(GitInspectionError):
                    inspect_git(tmp, timeout=10.0)


if __name__ == "__main__":
    unittest.main()
