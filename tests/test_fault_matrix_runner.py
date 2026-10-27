"""Process containment regressions for the opt-in fault injection runner.

All fixtures live in a temporary test root; no source checkout or service is
written to and no GitHub operations occur.
"""
from __future__ import annotations

import os
from pathlib import Path
import tempfile
import time
import unittest

from scripts.quality.fault_matrix import execute


def fixture(root: Path, source: str, name: str = "test_fake.py") -> tuple:
    folder = root / "tests"
    folder.mkdir(exist_ok=True)
    (folder / name).write_text(source, encoding="utf-8")
    return ("synthetic", "tests", name, "test_probe", ())


class FaultMatrixRunnerTests(unittest.TestCase):
    def test_exactly_one_pass_is_required(self) -> None:
        with tempfile.TemporaryDirectory(prefix="la-matrix-qa-") as tmp:
            root = Path(tmp)
            case = fixture(root,
                           "import unittest\n"
                           "class TestCase(unittest.TestCase):\n"
                           "    def test_probe(self): self.assertTrue(True)\n")
            self.assertEqual(execute(case, root=root)["status"], "PASS")
            case_missing = ("synthetic", "tests", "test_fake.py", "test_absent", ())
            self.assertEqual(execute(case_missing, root=root)["status"], "UNVERIFIED")

    def test_failing_selected_test_is_not_unverified_or_pass(self) -> None:
        with tempfile.TemporaryDirectory(prefix="la-matrix-qa-") as tmp:
            root = Path(tmp)
            case = fixture(root,
                           "import unittest\n"
                           "class TestCase(unittest.TestCase):\n"
                           "    def test_probe(self): self.fail('expected')\n")
            report = execute(case, root=root)
            self.assertEqual(report["status"], "FAIL")
            self.assertIn("expected", report["output_tail"])
            self.assertLessEqual(len(report["output_tail"]), 4096)

    def test_skipped_selected_test_is_unverified(self) -> None:
        with tempfile.TemporaryDirectory(prefix="la-matrix-qa-") as tmp:
            root = Path(tmp)
            case = fixture(root,
                           "import unittest\n"
                           "class TestCase(unittest.TestCase):\n"
                           "    @unittest.skip('native platform unavailable')\n"
                           "    def test_probe(self): pass\n")
            self.assertEqual(execute(case, root=root)["status"], "UNVERIFIED")

    @unittest.skipUnless(os.name == "posix", "process-group isolation requires POSIX")
    def test_timeout_kills_child_before_it_can_write_marker(self) -> None:
        with tempfile.TemporaryDirectory(prefix="la-matrix-qa-") as tmp:
            root = Path(tmp)
            marker = root / "escaped.txt"
            child = (
                "import time; from pathlib import Path; "
                f"time.sleep(1.5); Path({str(marker)!r}).write_text('orphan')"
            )
            case = fixture(root,
                           "import subprocess, sys, time, unittest\n"
                           "class TestCase(unittest.TestCase):\n"
                           "    def test_probe(self):\n"
                           f"        subprocess.Popen([sys.executable, '-c', {child!r}])\n"
                           "        time.sleep(30)\n")
            report = execute(case, root=root, timeout=1)
            self.assertEqual(report["status"], "UNVERIFIED")
            self.assertEqual(report["reason"], "timeout_group_killed")
            # The spawned child would create this marker if only the parent
            # unittest process had been terminated.
            time.sleep(1.8)
            self.assertFalse(marker.exists())


if __name__ == "__main__":
    unittest.main()
