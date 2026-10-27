"""Regression coverage for native macOS temp aliases and strict staging paths."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

from local_agent.repository.path_safety import has_untrusted_symlink


class PathSafetyTests(unittest.TestCase):
    def test_regular_temp_path_is_accepted(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            self.assertFalse(has_untrusted_symlink(Path(temp) / "staged.json"))

    def test_user_owned_ancestor_and_leaf_symlinks_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            real = root / "real"
            real.mkdir()
            alias = root / "alias"
            alias.symlink_to(real, target_is_directory=True)
            self.assertTrue(has_untrusted_symlink(alias / "host.json"))
            self.assertTrue(has_untrusted_symlink(alias))
            leaf = root / "identity.json"
            leaf.symlink_to(real)
            self.assertTrue(has_untrusted_symlink(leaf))

    @unittest.skipUnless(sys.platform == "darwin", "macOS-specific system alias")
    def test_native_macos_var_alias_is_ancestor_only(self) -> None:
        self.assertTrue(Path("/var").is_symlink())
        self.assertEqual(Path("/var").resolve(), Path("/private/var"))
        self.assertTrue(has_untrusted_symlink(Path("/var")))
        with tempfile.TemporaryDirectory() as temp:
            self.assertFalse(has_untrusted_symlink(Path(temp) / "identity.json"))


if __name__ == "__main__":
    unittest.main()
