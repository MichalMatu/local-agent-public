"""Sandbox readiness is hermetic and distinguishes UNVERIFIED from PASS."""
from __future__ import annotations

import io
import json
import unittest
from contextlib import redirect_stdout
from types import SimpleNamespace
from unittest import mock

from scripts.sandbox import doctor


class SandboxDoctorTests(unittest.TestCase):
    def test_ready_report_is_read_only_and_marks_native_gates_not_run(self) -> None:
        with mock.patch.object(doctor.verify_local, "missing_dependencies", return_value=[]), \
             mock.patch.object(doctor, "available_memory_mib", return_value=5000), \
             mock.patch.object(doctor.shutil, "disk_usage",
                               return_value=SimpleNamespace(free=7000 * doctor.MIB)):
            result = doctor.inspect()
        self.assertEqual(result["status"], "READY")
        self.assertEqual(result["memory_available_mib"], 5000)
        self.assertIn("native macOS smoke and launchd", result["not_run"])
        self.assertIn("installed Chrome and authenticated browser Send", result["not_run"])

    def test_memory_pressure_is_unverified_not_failing_product_code(self) -> None:
        with mock.patch.object(doctor.verify_local, "missing_dependencies", return_value=[]), \
             mock.patch.object(doctor, "available_memory_mib", return_value=1200), \
             mock.patch.object(doctor.shutil, "disk_usage",
                               return_value=SimpleNamespace(free=7000 * doctor.MIB)):
            report = doctor.inspect()
        self.assertEqual(report["status"], "UNVERIFIED")
        self.assertIn("available memory below sandbox budget", report["missing"])

    def test_unknown_memory_and_missing_modules_block_preflight(self) -> None:
        with mock.patch.object(doctor.verify_local, "missing_dependencies",
                               return_value=["httpx2"]), \
             mock.patch.object(doctor, "available_memory_mib", return_value=None), \
             mock.patch.object(doctor.shutil, "disk_usage",
                               return_value=SimpleNamespace(free=500 * doctor.MIB)):
            report = doctor.inspect()
        self.assertEqual(report["status"], "UNVERIFIED")
        self.assertIn("measurable available memory", report["missing"])
        self.assertIn("free disk below sandbox budget", report["missing"])
        self.assertIn("httpx2", report["missing"])

    def test_main_json_exit_code_is_semantic(self) -> None:
        stream = io.StringIO()
        with mock.patch.object(doctor, "inspect",
                               return_value={"status": "UNVERIFIED", "missing": ["ruff"]}), \
             redirect_stdout(stream):
            status = doctor.main(["--min-memory-mib", "1024"])
        self.assertEqual(status, 2)
        self.assertEqual(json.loads(stream.getvalue())["missing"], ["ruff"])
        with self.assertRaises(SystemExit):
            doctor.main(["--min-disk-mib", "0"])


if __name__ == "__main__":
    unittest.main()
