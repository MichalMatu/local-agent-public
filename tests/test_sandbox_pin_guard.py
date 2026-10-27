"""Keep source pin equality explicit in the strict offline bootstrap."""
import unittest
from pathlib import Path


class SandboxPinGuardTests(unittest.TestCase):
    def test_restore_requires_exact_wheelhouse_pins(self) -> None:
        root = Path(__file__).resolve().parents[1]
        script = (root / 'scripts/sandbox/restore-from-library.sh').read_text()
        self.assertIn('VERIFIED_OFFLINE_DEPENDENCY_PINS_MATCH_SOURCE', script)
        self.assertIn("tf.getmember('offline/requirements-linux-py313.txt')", script)
        self.assertIn('rebuild wheelhouse', script)