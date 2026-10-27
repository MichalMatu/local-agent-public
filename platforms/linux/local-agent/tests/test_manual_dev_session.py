"""Offline, non-service regressions for the operator-attended Linux MVP launcher."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


PRODUCT = Path(__file__).resolve().parents[1]
LAUNCHER = PRODUCT / "manual_dev_session.sh"


@unittest.skipIf(os.name != "posix" or not sys.platform.startswith("linux") or os.geteuid() == 0,
                 "requires an unprivileged native Linux test user")
class ManualSessionPreflightTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.root.chmod(0o700)
        self.release = self.root / "release"
        (self.release / "platforms/linux/local-agent").mkdir(parents=True)
        shutil.copytree(PRODUCT / "linux_agent", self.release / "platforms/linux/local-agent/linux_agent")
        shutil.copytree(PRODUCT.parent / "core" / "linux_agent",
                        self.release / "platforms/linux/core/linux_agent")
        for command in (
            ("git", "init", "-q", str(self.release)),
            ("git", "-C", str(self.release), "config", "user.name", "fixture"),
            ("git", "-C", str(self.release), "config", "user.email", "fixture@local.invalid"),
            ("git", "-C", str(self.release), "add", "."),
            ("git", "-C", str(self.release), "commit", "-q", "-m", "immutable source"),
        ):
            subprocess.run(command, check=True, capture_output=True)
        self.sha = subprocess.run(
            ("git", "-C", str(self.release), "rev-parse", "HEAD"),
            check=True, capture_output=True, text=True
        ).stdout.strip()
        self.state = self.root / "mvp"
        self.state.mkdir(mode=0o700)
        (self.state / "EMERGENCY_STOP").write_text("disabled\n")
        self.pilot = self.root / "pilot"
        self.pilot.mkdir(mode=0o700)
        (self.pilot / "EMERGENCY_STOP").write_text("disabled\n")
        self.config = self.root / "mvp.json"
        self.config.write_text(json.dumps({
            "version": 1, "host_id": "fixture-host", "installation_id": "fixture-install",
            "repository": "git@github.com:fixture/local-agent.git", "branch": "agent-control",
            "enabled": True, "host_intake": {
                "enabled": True, "authorization": "github_writers",
            },
        }))
        self.config.chmod(0o600)

    def run_check(self, *overrides: str) -> subprocess.CompletedProcess[str]:
        params = [
            str(self.config), str(self.state), str(self.release), self.sha,
            str(self.pilot / "EMERGENCY_STOP"), "fixture-host", "fixture-install"
        ]
        for i, value in enumerate(overrides):
            if value:
                params[i] = value
        return subprocess.run(
            ["bash", str(LAUNCHER), "--check", *params],
            env={**os.environ, "LINUX_AGENT_PYTHON": sys.executable},
            capture_output=True, text=True, timeout=20,
        )

    def test_check_success_preserves_both_stops_and_cannot_enable_intake(self) -> None:
        result = self.run_check()
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertIn("PREFLIGHT_PASS", result.stdout)
        self.assertIn("CHECK_ONLY", result.stdout)
        self.assertTrue((self.state / "EMERGENCY_STOP").exists())
        self.assertTrue((self.pilot / "EMERGENCY_STOP").exists())
        self.assertFalse((self.state / "claims").exists())
        self.assertFalse((self.state / "results").exists())

    def test_wrong_host_is_rejected_before_stop_removal(self) -> None:
        parameters = [""] * 5 + ["wrong-host"]
        result = self.run_check(*parameters)
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue((self.state / "EMERGENCY_STOP").exists())

    def test_wrong_source_sha_is_rejected(self) -> None:
        parameters = [""] * 3 + ["0" * 40]
        result = self.run_check(*parameters)
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue((self.state / "EMERGENCY_STOP").exists())

    def test_dirty_source_is_rejected(self) -> None:
        (self.release / "dirty-user-file").write_text("do not reset\n")
        result = self.run_check()
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue((self.release / "dirty-user-file").exists())
        self.assertTrue((self.state / "EMERGENCY_STOP").exists())

    def test_missing_pilot_stop_is_rejected(self) -> None:
        (self.pilot / "EMERGENCY_STOP").unlink()
        result = self.run_check()
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue((self.state / "EMERGENCY_STOP").exists())

    def test_missing_mvp_stop_is_rejected(self) -> None:
        (self.state / "EMERGENCY_STOP").unlink()
        result = self.run_check()
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.state / "claims").exists())
        self.assertFalse((self.state / "results").exists())


if __name__ == "__main__":
    unittest.main()
