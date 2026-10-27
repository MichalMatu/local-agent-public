"""Opt-in single-commit first-install template and independent cold rollback."""
from __future__ import annotations
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
import uuid

ROOT = Path(__file__).resolve().parents[4]


def run(*args, cwd=None, check=True):
    return subprocess.run(args, cwd=cwd, capture_output=True, text=True,
                          timeout=30, check=check)


@unittest.skipUnless(os.environ.get("LOCAL_AGENT_NATIVE_SYSTEMD") == "1",
                     "opt-in: isolated native user-systemd first installation")
class FirstInstallNativeTests(unittest.TestCase):
    def test_single_source_template_git_roundtrip_and_cold_stop(self):
        with tempfile.TemporaryDirectory(prefix="local-agent-first-install-") as temp:
            root = Path(temp)
            root.chmod(0o700)
            state = root / "state"
            state.mkdir(mode=0o700)
            stop = state / "EMERGENCY_STOP"
            stop.write_text("first-install fixture\n")
            bare, writer = root / "inbox.git", root / "publisher"
            run("git", "init", "--bare", str(bare))
            run("git", "clone", str(bare), str(writer))
            run("git", "-c", "user.name=Fixture", "-c", "user.email=fixture@local.invalid",
                "commit", "--allow-empty", "-m", "fixture", cwd=writer)
            run("git", "branch", "-M", "agent-control", cwd=writer)
            host, installation = "first-install-lab", str(uuid.uuid4())
            config = root / "config.json"
            config.write_text(json.dumps({
                "version": 1, "enabled": True, "host_id": host,
                "installation_id": installation, "repository": str(bare),
                "branch": "agent-control",
                "host_intake": {"enabled": True, "authorization": "github_writers"},
            }))
            config.chmod(0o600)
            ident = "first-install-" + uuid.uuid4().hex
            task = {"version": 1, "id": ident, "mode": "commands",
                    "target_host_id": host, "target_installation_id": installation,
                    "cwd": str(root), "commands": ["printf FIRST_INSTALL_OK"],
                    "resources": [], "command_timeout": 5, "idle_timeout": 5,
                    "task_timeout": 90, "memory_limit_mb": 128, "output_policy": "summary"}
            namespace = f".agent/hosts/{host}/supervisor/{installation}"
            request = writer / namespace / "tasks" / (ident + ".json")
            request.parent.mkdir(parents=True)
            request.write_text(json.dumps(task) + "\n")
            run("git", "add", ".", cwd=writer)
            run("git", "-c", "user.name=Fixture", "-c", "user.email=fixture@local.invalid",
                "commit", "-m", "task", cwd=writer)
            run("git", "push", "-u", "origin", "agent-control", cwd=writer)
            template = (ROOT / "deploy/linux/local-agent.service.example").read_text()
            unit = template.replace("/ABSOLUTE/SOURCE/SHA", str(ROOT))
            unit = unit.replace("/ABSOLUTE/PYTHON", str(Path(sys.executable).resolve()))
            unit = unit.replace("/ABSOLUTE/PRIVATE/config.json", str(config))
            unit = unit.replace("/ABSOLUTE/PRIVATE/state", str(state))
            name = "local-agent-first-install-lab-" + uuid.uuid4().hex[:16] + ".service"
            path = Path.home() / ".config/systemd/user" / name
            self.assertFalse(path.exists())
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(unit)
            try:
                run("systemd-analyze", "--user", "verify", str(path))
                run("systemctl", "--user", "daemon-reload")
                run("systemctl", "--user", "start", name)
                pid = int(run("systemctl", "--user", "show", name, "-p", "MainPID", "--value").stdout)
                self.assertGreater(pid, 1)
                time.sleep(1)
                self.assertFalse((state / "claims" / (ident + ".json")).exists())
                stop.unlink()
                result = state / "results" / (ident + ".json")
                deadline = time.monotonic() + 40
                remote = None
                while time.monotonic() < deadline:
                    value = run("git", "--git-dir", str(bare), "show",
                                "agent-control:" + namespace + "/task-results/" + ident + ".json",
                                check=False)
                    if value.returncode == 0:
                        remote = json.loads(value.stdout)
                        break
                    time.sleep(.25)
                self.assertIsNotNone(remote)
                self.assertEqual(remote, json.loads(result.read_text()))
                self.assertEqual(remote["result"]["status"], "done")
                self.assertEqual(remote["result"]["commands"][0]["output"], "FIRST_INSTALL_OK")
                self.assertEqual(len(list((state / "claims").glob("*.json"))), 1)
            finally:
                run("systemctl", "--user", "stop", name, check=False)
                self.assertEqual(path.read_text(), unit)
                path.unlink()
                run("systemctl", "--user", "daemon-reload")
                run("systemctl", "--user", "reset-failed", name, check=False)
            self.assertFalse(path.exists())
            self.assertNotEqual(run("systemctl", "--user", "is-active", name,
                                    check=False).returncode, 0)
