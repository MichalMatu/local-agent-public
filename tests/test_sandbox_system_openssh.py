"""Offline system OpenSSH bootstrap never trusts unknown package bytes."""
from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/sandbox/restore-system-openssh.sh"


class SandboxSystemOpenSSHTests(unittest.TestCase):
    def test_corrupted_package_fails_before_distro_or_root_checks(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            package = Path(temp) / "openssh-client.deb"
            package.write_bytes(b"!<arch>\\nnot-an-authentic-Debian-binary")
            result = subprocess.run(
                ["bash", str(SCRIPT), str(package)],
                cwd=ROOT, capture_output=True, text=True, timeout=10,
            )
        self.assertEqual(result.returncode, 2)
        self.assertIn("SHA256 mismatch", result.stderr)

    def test_missing_package_is_unverified_not_a_pass(self) -> None:
        result = subprocess.run(
            ["bash", str(SCRIPT), "/nonexistent/local-agent-ssh.deb"],
            cwd=ROOT, capture_output=True, text=True, timeout=10,
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("package missing", result.stdout)

    def test_pinned_official_hash_is_recorded_in_script(self) -> None:
        text = SCRIPT.read_text(encoding="utf-8")
        self.assertIn(
            "8fff343654f86c3b3266f94cd338c7b4da2e386da029327815118ea00bce03b9",
            text,
        )
        self.assertIn('PACKAGE_VERSION="1:10.0p1-7+deb13u4"', text)
        self.assertIn('== "$PACKAGE_VERSION"', text)
        # /etc/os-release contains VERSION, which must not overwrite the package pin.
        self.assertNotIn('== "$VERSION"', text)
        self.assertIn("OpenSSH_10.0p2", text)
        self.assertIn("Debian-7+deb13u4", text)
        self.assertIn("PINNED_DEBIAN_OPENSSH_READY", text)
        self.assertNotIn("apt install", text)
        self.assertNotIn("curl ", text)


if __name__ == "__main__":
    unittest.main()
