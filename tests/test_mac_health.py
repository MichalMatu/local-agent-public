"""Hermetic resource-admission checks; never execute native macOS commands."""
from __future__ import annotations

import subprocess
import unittest
from unittest import mock

from scripts import mac_health


SWAP_READY = "total = 8192.00M used = 1200.00M free = 6992.00M (encrypted)"
SWAP_PRESSURE = "total = 8192.00M used = 6937.56M free = 1254.44M (encrypted)"


class MacHealthTests(unittest.TestCase):
    def _inspect(self, *, load: float, swap: str) -> dict[str, object]:
        result = subprocess.CompletedProcess([], 0, stdout=swap)
        with mock.patch.object(mac_health.sys, "platform", "darwin"), \
             mock.patch.object(mac_health.os, "cpu_count", return_value=8), \
             mock.patch.object(mac_health.os, "getloadavg", return_value=(load, 6.0, 3.0)), \
             mock.patch.object(mac_health.subprocess, "run", return_value=result) as spawn:
            report = mac_health.check_resources()
        self.assertEqual(spawn.call_args.args[0], ["sysctl", "-n", "vm.swapusage"])
        return report

    def test_healthy_mac_is_ready(self) -> None:
        self.assertEqual(self._inspect(load=1.5, swap=SWAP_READY)["status"], "READY")

    def test_high_swap_is_telemetry_not_an_admission_failure(self) -> None:
        report = self._inspect(load=1.0, swap=SWAP_PRESSURE)
        self.assertEqual(report["status"], "READY")
        self.assertEqual(report["reasons"], [])
        self.assertEqual(report["swap_total_mib"], 8192)
        self.assertEqual(report["swap_used_mib"], 6938)
        self.assertIn("historical usage", report["warnings"][0])

    def test_high_load_is_telemetry_not_an_admission_failure(self) -> None:
        report = self._inspect(load=10.11, swap=SWAP_READY)
        self.assertEqual(report["status"], "READY")
        self.assertEqual(report["reasons"], [])
        self.assertIn("may lengthen verification stages", report["warnings"][0])

    def test_missing_swap_telemetry_does_not_block_native_checks(self) -> None:
        report = self._inspect(load=1.0, swap="unexpected")
        self.assertEqual(report["status"], "READY")
        self.assertIn("swap measurement unavailable", report["warnings"])

    def test_high_load_and_high_swap_are_both_nonblocking(self) -> None:
        report = self._inspect(load=12.0, swap=SWAP_PRESSURE)
        self.assertEqual(report["status"], "READY")
        self.assertEqual(len(report["warnings"]), 2)

    def test_linux_never_impersonates_native_mac(self) -> None:
        with mock.patch.object(mac_health.sys, "platform", "linux"), \
             mock.patch.object(mac_health.subprocess, "run") as spawn:
            report = mac_health.check_resources()
        self.assertEqual(report["status"], "UNVERIFIED")
        spawn.assert_not_called()

    def test_native_smoke_refuses_to_compile_when_preflight_fails(self) -> None:
        from scripts import verify

        blocked = subprocess.CompletedProcess([], 2)
        with mock.patch.object(verify.subprocess, "run", return_value=blocked), \
             mock.patch.object(verify, "compile_sources") as compile_sources:
            with self.assertRaises(SystemExit) as rejected:
                verify.run_macos_smoke()
        self.assertEqual(rejected.exception.code, 2)
        compile_sources.assert_not_called()

    def test_full_profile_blocks_before_stages_on_unhealthy_mac(self) -> None:
        from scripts import verify_local

        blocked = subprocess.CompletedProcess([], 2)
        with mock.patch.object(verify_local.sys, "platform", "darwin"), \
             mock.patch.object(verify_local, "exact_head", return_value="a" * 40), \
             mock.patch.object(verify_local, "missing_dependencies", return_value=[]), \
             mock.patch.object(verify_local.subprocess, "run", return_value=blocked), \
             mock.patch.object(verify_local, "planned_commands") as plan:
            status = verify_local.main(["--expected-sha", "a" * 40, "--profile", "full"])
        self.assertEqual(status, 2)
        plan.assert_not_called()

    def test_preflight_emits_exit_two_when_unverified(self) -> None:
        with mock.patch.object(mac_health, "check_resources",
                               return_value={"status": "UNVERIFIED", "reasons": ["load"]}):
            self.assertEqual(mac_health.main(), 2)


if __name__ == "__main__":
    unittest.main()
