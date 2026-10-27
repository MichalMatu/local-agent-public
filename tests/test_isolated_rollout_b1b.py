from __future__ import annotations

from pathlib import Path
import plistlib
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from scripts.isolated_rollout_controller import MARKER, MARKER_VALUE, RolloutRejected
from scripts.isolated_rollout_service_probe import (
    offline_systemd_watchdog_template,
    offline_macos_watchdog_plist,
    read_systemd_service,
)


class SystemdObservationTests(unittest.TestCase):
    def setUp(self):
        td = tempfile.TemporaryDirectory(prefix="la-rollout-service-test-")
        self.addCleanup(td.cleanup)
        self.home = Path(td.name)
        self.source = self.home / "source"
        self.source.mkdir()

    def reply(self, **changes):
        data = {
            "ActiveState": "active",
            "SubState": "running",
            "MainPID": "2345",
            "WorkingDirectory": str(self.source),
            "NeedDaemonReload": "no",
        }
        data.update(changes)
        return subprocess.CompletedProcess(["systemctl"], 0,
                                           "\n".join(f"{k}={v}" for k, v in data.items()) + "\n", "")

    def probe(self, response):
        return read_systemd_service(
            "local-agent.service", self.source,
            runner=lambda *args, **kwargs: response, verify_pid=False,
        )

    def test_exact_active_source_does_not_authorize_deployment(self):
        proof = self.probe(self.reply())
        self.assertEqual(proof["main_pid"], 2345)
        self.assertEqual(proof["working_directory"], str(self.source.resolve()))
        self.assertFalse(proof["source_switch_authorized"])
        self.assertFalse(proof["exclusive_queue_admission_fence"])

    def test_inactive_or_different_checkout_rejected(self):
        for changes in (
            {"ActiveState": "inactive"},
            {"SubState": "dead"},
            {"MainPID": "0"},
            {"MainPID": "invalid"},
            {"NeedDaemonReload": "yes"},
            {"WorkingDirectory": str(self.home)},
        ):
            with self.subTest(changes=changes):
                with self.assertRaises(RolloutRejected):
                    self.probe(self.reply(**changes))

    def test_duplicate_and_missing_unit_properties_rejected(self):
        with self.assertRaisesRegex(RolloutRejected, "duplicated"):
            reply = self.reply()
            reply.stdout += "MainPID=24\n"
            self.probe(reply)
        reply = self.reply()
        reply.stdout = reply.stdout.replace("NeedDaemonReload=no\n", "")
        with self.assertRaisesRegex(RolloutRejected, "incomplete"):
            self.probe(reply)

    def test_command_bound_and_invalid_name(self):
        with self.assertRaisesRegex(RolloutRejected, "explicit systemd"):
            read_systemd_service("local-agent.service;reboot", self.source)
        with self.assertRaisesRegex(RolloutRejected, "inspection failed"):
            self.probe(subprocess.CompletedProcess([], 1, "", "failed"))

    def test_process_lookup_or_permission_refuses_service_liveness(self):
        with mock.patch("scripts.isolated_rollout_service_probe.os.kill", side_effect=ProcessLookupError):
            with self.assertRaisesRegex(RolloutRejected, "MainPID unavailable"):
                read_systemd_service("local-agent.service", self.source,
                                     runner=lambda *a, **kw: self.reply())

    def test_offline_unit_is_not_installed(self):
        root = self.home / "la-rollout-synthetic"
        root.mkdir()
        (root / MARKER).write_text(MARKER_VALUE)
        # The offline Linux template requires paths without spaces; a Mac
        # QA venv can live in Application Support while its OS binary does not.
        path = Path(sys.executable).resolve()
        unit = offline_systemd_watchdog_template(
            sandbox=root, python_executable=path, checkout=self.source,
        )
        self.assertIn("Type=oneshot", unit)
        self.assertIn("NoNewPrivileges=yes", unit)
        self.assertIn("scripts.isolated_rollout_watchdog", unit)
        self.assertFalse((root / "watchdog.service").exists())

    def test_disabled_macos_launchd_rehearsal_plist(self):
        root = self.home / "la-rollout-macos"
        root.mkdir()
        (root / MARKER).write_text(MARKER_VALUE)
        payload = plistlib.loads(offline_macos_watchdog_plist(
            sandbox=root, python_executable=Path(sys.executable),
            checkout=self.source,
        ))
        self.assertTrue(payload["Disabled"])
        self.assertFalse(payload["RunAtLoad"])
        self.assertEqual(payload["StartInterval"], 30)
        self.assertEqual(payload["ProgramArguments"][2],
                         "scripts.isolated_rollout_watchdog")
        self.assertFalse((root / "watchdog.plist").exists())

    def test_watchdog_template_refuses_wrong_sandbox(self):
        root = self.home / "normal-folder"
        root.mkdir()
        with self.assertRaisesRegex(RolloutRejected, "prefix"):
            offline_systemd_watchdog_template(
                sandbox=root, python_executable=Path(sys.executable), checkout=self.source,
            )


if __name__ == "__main__":
    unittest.main()
