"""Offline Pi source gate regressions: no real systemd, SSH, or B2 effects."""
from __future__ import annotations

import io
import json
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from pi_agent import native_admission as gate


class PiNativeAdmissionTests(unittest.TestCase):
    def setUp(self):
        self.platform = {
            "platform": "raspberry-pi-zero-2-w", "arch": "aarch64"
        }
        self.b2 = {"LoadState": "loaded", "ActiveState": "active",
                   "MainPID": "428429"}
        self.candidate = {"LoadState": "not-found", "ActiveState": "inactive",
                          "MainPID": "0"}

    def check(self, **changes):
        args = {"platform": self.platform, "b2": self.b2,
                "candidate": self.candidate, "available_kib": 130 * 1024}
        args.update(changes)
        return gate.check_source_test_admission(**args)

    def test_only_historical_b2_running_may_admit_source_tests(self):
        result = self.check()
        self.assertEqual(result["status"], "source_test_admitted_not_deployed")
        self.assertEqual(result["b2_main_pid"], 428429)
        self.assertFalse(result["candidate_active"])
        self.assertFalse(result["physical_project_e2e_verified"])
        self.assertFalse(result["rollback_verified"])

    def test_old_b2_missing_or_unconfirmed_blocks(self):
        for changed in (
            {"ActiveState": "inactive"}, {"LoadState": "not-found"},
            {"MainPID": "0"}, {"MainPID": "not-a-number"},
        ):
            with self.subTest(changed=changed), self.assertRaises(
                gate.NativeAdmissionBlocked
            ):
                self.check(b2=self.b2 | changed)

    def test_second_pi_agent_active_or_unknown_blocks(self):
        for changed in (
            {"ActiveState": "active", "MainPID": "500"},
            {"ActiveState": "activating"}, {"MainPID": "15"},
            {"LoadState": "masked"}, {"LoadState": "bad"},
        ):
            with self.subTest(changed=changed), self.assertRaises(
                gate.NativeAdmissionBlocked
            ):
                self.check(candidate=self.candidate | changed)

    def test_low_or_ambiguous_ram_and_wrong_hardware_block(self):
        for available in (0, 64 * 1024, gate.MIN_TEST_AVAILABLE_KIB - 1, None, True):
            with self.subTest(value=available), self.assertRaises(
                gate.NativeAdmissionBlocked
            ):
                self.check(available_kib=available)
        with self.assertRaises(gate.NativeAdmissionBlocked):
            self.check(platform=self.platform | {"arch": "armv7l"})
        self.assertEqual(self.check(available_kib=gate.MIN_TEST_AVAILABLE_KIB)["status"],
                         "source_test_admitted_not_deployed")

    def test_systemd_queries_are_read_only_and_ambiguous_fails_closed(self):
        seen = []

        def runner(command, **kwargs):
            seen.append(command)
            return subprocess.CompletedProcess(
                command, 0,
                "LoadState=loaded\nActiveState=active\nMainPID=428429\n", "")

        result = gate._systemctl("--system", gate.SYSTEM_UNIT, runner=runner)
        self.assertEqual(result, self.b2)
        self.assertEqual(seen[0][:4],
                         ["systemctl", "--system", "show", "local-agent.service"])
        self.assertFalse(any(x in seen[0] for x in (
            "start", "stop", "restart", "enable", "disable", "daemon-reload"
        )))
        with self.assertRaises(gate.NativeAdmissionBlocked):
            gate._systemctl("--user", gate.CANDIDATE_UNIT,
                            runner=lambda c, **k: subprocess.CompletedProcess(c, 1, "", ""))

    def test_duplicate_status_and_missing_field_fail_closed(self):
        for content in (
            "LoadState=loaded\nActiveState=active\n",
            "LoadState=loaded\nLoadState=loaded\nActiveState=active\nMainPID=1\n",
            "LoadState=loaded\nActiveState=active\nMainPID=notnumber\n",
        ):
            with self.subTest(content=content), self.assertRaises(
                gate.NativeAdmissionBlocked
            ):
                gate._systemctl("--system", gate.SYSTEM_UNIT,
                                runner=lambda c, **k: subprocess.CompletedProcess(c, 0, content, ""))

    def test_meminfo_parser_requires_a_unique_numeric_value(self):
        with tempfile.TemporaryDirectory() as tmp:
            file = Path(tmp) / "meminfo"
            file.write_text("MemTotal: 400000 kB\nMemAvailable: 130000 kB\n")
            self.assertEqual(gate._available_kib(meminfo=file), 130000)
            for content in (
                "MemTotal: 400000 kB\n", "MemAvailable: unknown kB\n",
                "MemAvailable: 130000 MiB\n",
                "MemAvailable: 130000 kB\nMemAvailable: 100000 kB\n",
            ):
                with self.subTest(content=content), self.assertRaises(
                    gate.NativeAdmissionBlocked
                ):
                    file.write_text(content)
                    gate._available_kib(meminfo=file)

    def test_cli_status_is_explicit_and_does_not_claim_deployment(self):
        with patch.object(gate, "inspect", return_value=self.check()), redirect_stdout(
            io.StringIO()
        ) as out:
            self.assertEqual(gate.main(), 0)
        payload = json.loads(out.getvalue())
        self.assertEqual(payload["status"], "source_test_admitted_not_deployed")
        with patch.object(gate, "inspect",
                          side_effect=gate.NativeAdmissionBlocked("second service")), redirect_stderr(
                              io.StringIO()
                          ) as err:
            self.assertEqual(gate.main(), 2)
        self.assertEqual(json.loads(err.getvalue())["status"], "source_test_blocked")


if __name__ == "__main__":
    unittest.main()
