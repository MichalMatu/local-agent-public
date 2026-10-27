"""Offline, nondestructive Raspberry Pi Zero 2 W port checks."""
from __future__ import annotations

import io
from contextlib import redirect_stderr
from pathlib import Path
from unittest import TestCase, mock
import tempfile

from linux_agent.service_plan import ReleasePair
from pi_agent.preflight import UnsupportedPi, validate_platform
from pi_agent.service_plan import UNIT_NAME, plan_release_pair, render_user_unit


class PiPortTests(TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        releases = root / "releases"
        a, b = "a" * 40, "b" * 40
        for sha in (a, b):
            for pkg in (
                "platforms/raspberry-pi/local-agent/pi_agent",
                "platforms/linux/core/linux_agent",
            ):
                p = releases / sha / pkg
                p.mkdir(parents=True)
                (p / "__main__.py").write_text("# fixture\n")
        for sha in (a, b):
            bridge = releases / sha / "platforms/linux/local-agent/linux_agent/__init__.py"
            bridge.parent.mkdir(parents=True)
            bridge.write_text("# source-local adapter fixture\n")
        python = root / "venv/bin/python"
        python.parent.mkdir(parents=True)
        python.write_text("# python stub\n")
        config = root / "private/config.json"
        config.parent.mkdir()
        config.write_text("{}")
        config.chmod(0o600)
        state = root / "state"
        state.mkdir(mode=0o700)
        self.pair = ReleasePair(releases / a, a, releases / b, b,
                                python, config, state)

    def test_supported_exact_arm64_pi_zero_2w(self):
        data = validate_platform(system="Linux", machine="aarch64",
                                 model="Raspberry Pi Zero 2 W Rev 1.0",
                                 os_id="debian")
        self.assertEqual(data["platform"], "raspberry-pi-zero-2-w")

    def test_unsupported_arch_models_and_os_fail_closed(self):
        good = dict(system="Linux", machine="aarch64",
                    model="Raspberry Pi Zero 2 W Rev 1.0", os_id="raspbian")
        for change in (
            {"system": "Darwin"}, {"machine": "armv7l"},
            {"machine": "armv6l"}, {"machine": "x86_64"},
            {"model": "Raspberry Pi Zero W Rev 1.1"},
            {"model": "Raspberry Pi 5 Model B Rev 1.0"},
            {"os_id": "ubuntu"},
        ):
            with self.subTest(change=change), self.assertRaises(UnsupportedPi):
                validate_platform(**(good | change))

    def test_pi_launcher_does_not_enter_linux_runtime_on_wrong_host(self):
        from pi_agent.__main__ import main
        with mock.patch("pi_agent.__main__.read_host", side_effect=UnsupportedPi("wrong host")), \
             mock.patch("linux_agent.__main__.main") as linux_main, \
             mock.patch("sys.argv", ["pi_agent", "--config", "x", "--state", "y", "serve"]), \
             redirect_stderr(io.StringIO()):
            self.assertEqual(main(), 2)
            linux_main.assert_not_called()

    def test_correct_host_delegates_only_to_linux_entrypoint(self):
        from pi_agent.__main__ import main
        with mock.patch("pi_agent.__main__.read_host", return_value={}), \
             mock.patch("linux_agent.__main__.main", return_value=0) as linux_main, \
             mock.patch("sys.argv", ["pi_agent", "--config", "x", "--state", "y", "status"]):
            self.assertEqual(main(), 0)
            linux_main.assert_called_once_with()

    def test_dry_run_has_independent_pi_namespace_and_no_effect(self):
        result = plan_release_pair(self.pair)
        self.assertEqual(result["status"], "plan_only_not_installed")
        self.assertEqual(result["unit_name"], UNIT_NAME)
        self.assertNotEqual(result["candidate_unit"], result["rollback_unit"])
        for unit in (result["candidate_unit"], result["rollback_unit"]):
            self.assertIn(" -B -m pi_agent ", unit)
            self.assertIn("KillMode=control-group", unit)
            self.assertIn("MemoryMax=320M", unit)
            self.assertIn("TasksMax=32", unit)
            self.assertIn("LOCAL_AGENT_MEMORY_LIMIT_MB_MAX=320", unit)
            self.assertIn("platforms/linux/core", unit)
            self.assertIn("platforms/linux/local-agent", unit)
            self.assertIn("platforms/raspberry-pi/local-agent", unit)
            self.assertNotIn("platforms/macos", unit)
            self.assertNotIn("User=root", unit)
            self.assertNotIn("systemctl", unit)
        self.assertFalse((self.pair.state / UNIT_NAME).exists())

    def test_missing_pi_code_and_invalid_caps_refuse(self):
        (self.pair.candidate / "platforms/raspberry-pi/local-agent/pi_agent/__main__.py").unlink()
        with self.assertRaises(ValueError):
            render_user_unit(self.pair, candidate=True)
        self.assertIn(" -B -m pi_agent ", render_user_unit(self.pair, candidate=False))
        with self.assertRaises(ValueError):
            render_user_unit(self.pair, candidate=False, memory_max_mb=500)

    def test_unsafe_config_and_wrong_release_pair_rejected(self):
        self.pair.config.chmod(0o644)
        with self.assertRaises(ValueError):
            plan_release_pair(self.pair)


if __name__ == "__main__":
    import unittest
    unittest.main()
