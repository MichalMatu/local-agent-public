"""Opt-in physical Debian systemd/cgroup acceptance. Never installs a unit.

Run with LOCAL_AGENT_NATIVE_SYSTEMD=1 on the designated unprivileged Debian
account. All Git, claim and process fixtures are unique, offline and disposable.
No production configuration, services or task namespace are touched.
"""
from __future__ import annotations

# ruff: noqa: E402 -- local source import setup in standalone test

import json
import os
from pathlib import Path
import shlex
import signal
import subprocess
import sys
import tempfile
import time
import unittest
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from linux_agent.agent import Agent


def _run(*args: str, cwd: Path | None = None, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        list(args), cwd=cwd, check=check, capture_output=True, text=True, timeout=30,
    )


def _git(*args: str, cwd: Path | None = None) -> str:
    return _run("git", *(("-C", str(cwd)) if cwd else ()), *args).stdout.strip()


def _alive(pid: int) -> bool:
    try:
        state = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0]
        return state not in {"Z", "X"}
    except (FileNotFoundError, ProcessLookupError):
        return False


def _await(check, timeout: float, what: str) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if check():
            return
        time.sleep(0.05)
    raise AssertionError("timed out awaiting " + what)


@unittest.skipUnless(os.environ.get("LOCAL_AGENT_NATIVE_SYSTEMD") == "1",
                     "opt-in only: requires disposable physical Debian systemd test")
class NativeSystemdCgroupTests(unittest.TestCase):
    def test_main_sigkill_cleans_fd_detached_descendant_and_no_replay(self):
        self.assertEqual(_run("stat", "-fc", "%T", "/sys/fs/cgroup").stdout.strip(),
                         "cgroup2fs", "native test requires unified cgroup v2")
        self.assertEqual(_run("systemctl", "--user", "is-active",
                              "default.target").returncode, 0)
        with tempfile.TemporaryDirectory(prefix="linux-agent-cgroup-") as tmp:
            root = Path(tmp)
            bare = root / "control.git"
            writer = root / "publisher"
            _git("init", "--bare", str(bare))
            _git("clone", str(bare), str(writer))
            _git("-c", "user.name=Fixture", "-c", "user.email=fixture@local.invalid",
                 "commit", "--allow-empty", "-m", "init", cwd=writer)
            _git("branch", "-M", "agent-control", cwd=writer)
            _git("push", "-u", "origin", "agent-control", cwd=writer)
            installation = str(uuid.uuid4())
            host = "test-cgroup-" + uuid.uuid4().hex[:12]
            config = {
                "version": 1, "host_id": host, "installation_id": installation,
                "repository": str(bare), "branch": "agent-control",
                "enabled": True,
                "host_intake": {"enabled": True, "authorization": "github_writers"},
            }
            state = root / "private-state"
            config_file = root / "config.json"
            config_file.write_text(json.dumps(config))
            config_file.chmod(0o600)
            task_id = "cgroup-kill-once"
            marker = root / "once-marker"
            pidfile = root / "detached-pid"
            started = root / "command-started"
            # The detached grandchild deliberately drops every inherited FD,
            # including the Mac-derived task lease, and establishes a new session.
            # It must STILL remain in the unit's kernel cgroup.
            command_source = (
                "import os,time\n"
                "from pathlib import Path\n"
                f"with Path({str(marker)!r}).open('a') as f: f.write('x')\n"
                "p=os.fork()\n"
                "if p==0:\n"
                " os.setsid()\n"
                " q=os.fork()\n"
                " if q: os._exit(0)\n"
                " for fd in range(3,256):\n"
                "  try: os.close(fd)\n"
                "  except OSError: pass\n"
                f" Path({str(pidfile)!r}).write_text(str(os.getpid()))\n"
                " while True: time.sleep(1)\n"
                "os.waitpid(p,0)\n"
                f"Path({str(started)!r}).write_text('ready')\n"
                "while True: time.sleep(1)\n"
            )
            task = {
                "version": 1, "id": task_id, "target_host_id": host,
                "target_installation_id": installation, "cwd": str(root),
                "commands": ["sh -c " + shlex.quote(
                    shlex.quote(sys.executable) + " -c " + shlex.quote(command_source)
                )], "resources": [],
                "command_timeout": 30, "task_timeout": 100, "idle_timeout": 30,
                "memory_limit_mb": 256, "output_policy": "summary",
            }
            dest = writer / f".agent/hosts/{host}/supervisor/{installation}/tasks/{task_id}.json"
            dest.parent.mkdir(parents=True)
            dest.write_text(json.dumps(task))
            _git("add", "--", str(dest.relative_to(writer)), cwd=writer)
            _git("-c", "user.name=Fixture", "-c", "user.email=fixture@local.invalid",
                 "commit", "-m", "immutable offline task", cwd=writer)
            _git("push", cwd=writer)
            unit = "la-cgroup-native-" + uuid.uuid4().hex[:16] + ".service"
            detached_pid: int | None = None
            detached_pidfd: int | None = None
            try:
                # Transient user service; --collect removes it when dead. Neither
                # a persistent .service file nor a timer or enabled unit is created.
                _run(
                    "systemd-run", "--user", "--collect", "--unit=" + unit,
                    "--property=Type=exec", "--property=KillMode=control-group",
                    "--property=SendSIGKILL=yes", "--property=Restart=no",
                    "--property=TimeoutStopSec=5s",
                    "--setenv=PYTHONPATH=" + str(ROOT),
                    sys.executable, "-m", "linux_agent", "--config", str(config_file),
                    "--state", str(state), "serve",
                )
                try:
                    _await(lambda: started.is_file() and pidfile.is_file(), 12,
                           "service to start detached grandchild")
                except AssertionError as exc:
                    journal = _run("journalctl", "--user", "--unit=" + unit,
                                   "--no-pager", "--lines=35", check=False)
                    show = _run("systemctl", "--user", "show", unit,
                                "--property=ActiveState,SubState,Result,ExecMainStatus",
                                check=False)
                    spooled = list((state / "results").glob("*.json"))
                    debug = [f"{p.name}: {p.read_text()[:1500]}" for p in spooled]
                    raise AssertionError(
                        f"{exc}; show={show.stdout[-1200:]}; "
                        f"journal={journal.stdout[-2500:]}; "
                        f"spooled={debug}"
                    ) from exc
                detached_pid = int(pidfile.read_text())
                self.assertTrue(_alive(detached_pid), "detached process did not survive fork")
                cg = _run("systemctl", "--user", "show", unit,
                          "--property=ControlGroup", "--value").stdout.strip()
                self.assertTrue(cg.startswith("/"), "missing service cgroup")
                member_cg = Path(f"/proc/{detached_pid}/cgroup").read_text()
                self.assertIn("0::" + cg + "\n", member_cg,
                              "FD-dropping detached grandchild escaped service cgroup")
                mainpid = int(_run("systemctl", "--user", "show", unit,
                                   "--property=MainPID", "--value").stdout.strip())
                self.assertGreater(mainpid, 1)
                self.assertNotEqual(mainpid, detached_pid)
                self.assertEqual(marker.read_text(), "x")
                if hasattr(os, "pidfd_open"):
                    detached_pidfd = os.pidfd_open(detached_pid, 0)
                # Kill ONLY the transient unit's main process. systemd must
                # kill its remaining children regardless of sessions or FDs.
                _run("systemctl", "--user", "kill", "--kill-whom=main",
                     "--signal=SIGKILL", unit)
                _await(lambda: not _alive(detached_pid), 12,
                       "systemd control-group cleanup of detached child")
                # A separately launched recovery runner uses the SAME durable
                # claim but must never execute the command a second time.
                agent = Agent(config, state)
                self.assertEqual(agent.once()["status"], "idle")
                self.assertEqual(agent.once()["status"], "idle")
                self.assertEqual(marker.read_text(), "x")
                _git("pull", "--ff-only", cwd=writer)
                result_path = writer / (
                    f".agent/hosts/{host}/supervisor/{installation}"
                    f"/task-results/{task_id}.json"
                )
                result = json.loads(result_path.read_text())
                self.assertEqual(result["status"], "unknown")
                self.assertEqual(result["result"]["reason"],
                                 "interrupted_or_uncertain_execution_no_replay")
            finally:
                # Stop only our uniquely named transient unit. If containment
                # failed, independently terminate only the PID we observed.
                _run("systemctl", "--user", "stop", unit, check=False)
                if detached_pidfd is not None:
                    if detached_pid is not None and _alive(detached_pid):
                        signal.pidfd_send_signal(detached_pidfd, signal.SIGKILL)
                    os.close(detached_pidfd)
                elif detached_pid is not None and _alive(detached_pid):
                    # Refuse unsafe PID-only cleanup if pidfd was unavailable.
                    raise AssertionError("detached process survived without safe pidfd cleanup")


if __name__ == "__main__":
    unittest.main()
