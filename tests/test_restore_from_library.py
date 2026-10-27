"""Offline sandbox bootstrap rejects stale, missing and unsafe input before effects."""
from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/sandbox/restore-from-library.sh"
SHA = "a" * 40


class RestoreFromLibraryTests(unittest.TestCase):
    def _call(self, dest: Path, *, sha: str = SHA) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["bash", str(SCRIPT), sha, "/nonexistent/source",
             "/nonexistent/wheels.tgz", "/nonexistent/wheels.tgz.sha256",
             "/nonexistent/openssh-client.deb", str(dest)],
            cwd=ROOT, capture_output=True, text=True, timeout=12,
        )

    def test_invalid_sha_is_unverified(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            result = self._call(Path(temp) / "new", sha="invalid")
        self.assertEqual(result.returncode, 2)
        self.assertIn("invalid source SHA", result.stderr)

    def test_existing_destination_is_never_overwritten(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            target = Path(temp) / "kept"
            target.mkdir()
            (target / "marker").write_text("keep", encoding="utf-8")
            result = self._call(target)
            self.assertEqual(result.returncode, 2)
            self.assertIn("destination exists", result.stderr)
            self.assertEqual((target / "marker").read_text(), "keep")

    def test_missing_offline_inputs_are_not_accepted(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            result = self._call(Path(temp) / "new")
        self.assertEqual(result.returncode, 2)
        self.assertFalse((Path(temp) / "new").exists())

    def test_no_downloads_and_tar_member_checks_remain_explicit(self) -> None:
        source = SCRIPT.read_text(encoding="utf-8")
        self.assertIn("VERIFIED_SOURCE_WHEELS_AND_DEBIAN", source)
        self.assertIn("tf.extractall(target, filter='data')", source)
        self.assertIn("not member.isfile() and not member.isdir()", source)
        self.assertIn("SANDBOX_READY", source)
        self.assertIn("scripts/sandbox/doctor.py", source)
        self.assertNotIn("pip download", source)
        self.assertNotIn("apt install", source)


if __name__ == "__main__":
    unittest.main()
