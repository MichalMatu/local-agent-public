"""Real disposable launchd proof for CLI polling source rollout and rollback.

Opt in with LOCAL_AGENT_MVP_DISPOSABLE_LAUNCHD_TEST=1. Never address the
installed com.michal.local-agent service in a test or stage production config.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import plistlib
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
import uuid

from scripts import macos_cli_polling_rollout as release


@unittest.skipUnless(
    sys.platform == "darwin" and
    os.environ.get("LOCAL_AGENT_MVP_DISPOSABLE_LAUNCHD_TEST") == "1",
    "explicit opt-in for real disposable launchd testing",
)
class RealDisposableMvpCutoverTests(unittest.TestCase):
    def setUp(self) -> None:
        t = tempfile.TemporaryDirectory(prefix="la-mvp-real-launchd-")
        self.addCleanup(t.cleanup)
        self.home = Path(t.name)
        self.root = self.home / release.RELEASE_NAME
        self.root.mkdir(parents=True)
        self.state = self.home / "private-state"
        self.state.mkdir()
        self.baseline = self.root / "baseline-host-mvp"
        self.candidate = self.root / "candidate-cli"
        self.baseline.mkdir()
        self.candidate.mkdir()
        suffix = uuid.uuid4().hex
        self.label = "dev.localagent.mvp.disposable." + suffix
        self.watch_label = "dev.localagent.mvp.watch." + suffix
        self.service_target = f"gui/{os.getuid()}/{self.label}"
        self.watch_target = f"gui/{os.getuid()}/{self.watch_label}"
        self.domain = f"gui/{os.getuid()}"
        self.live_plist = self.home / "live-service.plist"
        self.watch_plist = self.home / "watcher.plist"
        self.watch_out = self.home / "watch.out"
        self.watch_err = self.home / "watch.err"
        self.loaded_service = False
        self.loaded_watch = False
        self.addCleanup(self.cleanup_launchd)

    def launchctl(self, *args: str, timeout: int = 20) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["/bin/launchctl", *args], capture_output=True, text=True,
            timeout=timeout,
        )

    def cleanup_launchd(self) -> None:
        if self.loaded_watch:
            self.launchctl("bootout", self.watch_target)
        if self.loaded_service:
            self.launchctl("bootout", self.service_target)

    def make_service(self, directory: Path, revision: str, *, heartbeat: bool) -> None:
        script = directory / "agent_entrypoint.py"
        script.write_text(
            "import json,time\n"
            "from pathlib import Path\n"
            "from datetime import datetime,timezone\n"
            f"status=Path({str(self.state / 'status.json')!r})\n"
            f"rev={revision!r}\n"
            f"heartbeat={heartbeat!r}\n"
            "while True:\n"
            "    if heartbeat:\n"
            "        stamp=datetime.now(timezone.utc).isoformat()\n"
            "        status.write_text(json.dumps({'state':'running','self_revision':rev,'updated_at':stamp}))\n"
            "    time.sleep(.2)\n"
        )

    def setup_fixture(self, *, candidate_heartbeat: bool) -> None:
        self.make_service(self.baseline, release.BASE_SHA, heartbeat=True)
        self.make_service(self.candidate, release.CANDIDATE_SHA,
                          heartbeat=candidate_heartbeat)
        program = [sys.executable, str(self.baseline / "agent_entrypoint.py")]
        old = {
            "Label": self.label, "ProgramArguments": program,
            "WorkingDirectory": str(self.baseline),
            "RunAtLoad": True, "KeepAlive": True,
            "ThrottleInterval": 1,
        }
        new = dict(old)
        new["ProgramArguments"] = [sys.executable, str(self.candidate / "agent_entrypoint.py")]
        new["WorkingDirectory"] = str(self.candidate)
        old_bytes = plistlib.dumps(old)
        new_bytes = plistlib.dumps(new)
        self.live_plist.write_bytes(old_bytes)
        (self.root / "mvp-baseline.plist").write_bytes(old_bytes)
        (self.root / "mvp-candidate.plist").write_bytes(new_bytes)
        selected = (json.dumps({"version": 1, "enabled": True,
                                "host_id": release.HOST_ID, "host_intake":
                                {"enabled": True, "authorization": "github_writers"}},
                               sort_keys=True) + "\n").encode()
        (self.root / "mvp-host-config.json").write_bytes(selected)
        (self.state / "supervisor-control.json").write_bytes(selected)
        backup = self.root / "configuration-backup"
        backup.mkdir()
        (backup / "supervisor-control.json").write_bytes(selected)
        (self.root / "staging-manifest.json").write_text(json.dumps({
            "baseline_source_path": str(self.baseline),
        }))
        (self.state / release.LOCK).write_bytes(b"")
        # The source/controller code is stored outside both candidate and baseline.
        controller = self.root / "mvp-controller.py"
        shutil.copy2(Path(release.__file__), controller)
        journal = {
            "version": 1, "state": "prepared",
            "baseline_sha": release.BASE_SHA, "candidate_sha": release.CANDIDATE_SHA,
            "plist_baseline_sha256": hashlib.sha256(old_bytes).hexdigest(),
            "plist_candidate_sha256": hashlib.sha256(new_bytes).hexdigest(),
            "controller_sha256": hashlib.sha256(controller.read_bytes()).hexdigest(),
        }
        (self.root / release.JOURNAL).write_text(json.dumps(journal))
        # This wrapper is an independent launchd job, not a worker subprocess.
        # Only the ephemeral label is patched; production label is never used.
        wrapper = self.home / "isolated-recovery-wrapper.py"
        wrapper.write_text(
            "import json,sys\n"
            "from pathlib import Path\n"
            "import importlib.util\n"
            "from unittest import mock\n"
            f"spec=importlib.util.spec_from_file_location('independent_mvp_release',{str(controller)!r})\n"
            "from contextlib import ExitStack\n"
            "module=importlib.util.module_from_spec(spec)\n"
            "spec.loader.exec_module(module)\n"
            f"root=Path({str(self.root)!r})\n"
            f"state=Path({str(self.state)!r})\n"
            f"candidate=Path({str(self.candidate)!r})\n"
            f"plist=Path({str(self.live_plist)!r})\n"
            f"label={self.label!r}\n"
            "with mock.patch.object(module,'_validate_paths',return_value=(root,state,candidate,plist)),"
            "mock.patch.object(module,'_queue_idle',return_value=True),"
            "mock.patch.object(module,'_execution_fence',return_value=ExitStack()),"
            "mock.patch.object(module,'_git',side_effect=lambda path,*a: (module.BASE_SHA if path.name=='baseline-host-mvp' else module.CANDIDATE_SHA) if a[0]=='rev-parse' else ''),"
            "mock.patch.object(module,'LABEL',label):\n"
            "    print(json.dumps(module.watch(root.parent,health_timeout_seconds=5)),flush=True)\n"
        )
        self.watch_plist.write_bytes(plistlib.dumps({
            "Label": self.watch_label,
            "ProgramArguments": [sys.executable, "-B", str(wrapper)],
            "WorkingDirectory": str(Path(release.__file__).resolve().parents[1]),
            "RunAtLoad": True, "KeepAlive": False,
            "StandardOutPath": str(self.watch_out),
            "StandardErrorPath": str(self.watch_err),
        }))
        begin = self.launchctl("bootstrap", self.domain, str(self.live_plist))
        self.assertEqual(begin.returncode, 0, begin.stderr)
        self.loaded_service = True
        end = time.monotonic() + 15
        while time.monotonic() < end:
            if self.launchctl("print", self.service_target).returncode == 0:
                break
            time.sleep(.1)
        else:
            self.fail("disposable B1c did not launch")

    def read_watch_outcome(self) -> dict:
        end = time.monotonic() + 22
        while time.monotonic() < end:
            if self.watch_out.exists() and self.watch_out.stat().st_size:
                try:
                    return json.loads(self.watch_out.read_text())
                except (UnicodeError, ValueError):
                    pass
            if (self.root / release.JOURNAL).is_file():
                journal = json.loads((self.root / release.JOURNAL).read_text())
                if journal["state"] == "rolled_back" and self.watch_err.exists():
                    break
            time.sleep(.1)
        error = self.watch_err.read_text() if self.watch_err.exists() else "(no stderr)"
        self.fail("independent launchd job did not publish result: " + error[-2500:])

    def start_external_watchdog(self) -> None:
        started = self.launchctl("bootstrap", self.domain, str(self.watch_plist))
        self.assertEqual(started.returncode, 0, started.stderr)
        self.loaded_watch = True

    def test_external_launchd_accepts_healthy_candidate_without_replaying_tasks(self) -> None:
        self.setup_fixture(candidate_heartbeat=True)
        self.start_external_watchdog()
        outcome = self.read_watch_outcome()
        self.assertEqual(outcome["state"], "accepted")
        self.assertEqual(json.loads((self.root / release.JOURNAL).read_text())["state"],
                         "accepted")
        self.assertEqual(self.launchctl("print", self.service_target).returncode, 0)
        self.assertFalse((self.state / release.FROZEN).exists())
        self.assertEqual(self.live_plist.read_bytes(),
                         (self.root / "mvp-candidate.plist").read_bytes())

    def test_external_launchd_rolls_back_unhealthy_candidate(self) -> None:
        self.setup_fixture(candidate_heartbeat=False)
        self.start_external_watchdog()
        # An unhealthy candidate deliberately makes watch fail, but must restore
        # the original service from a stable controller, not replay commands.
        end = time.monotonic() + 22
        while time.monotonic() < end:
            journal = json.loads((self.root / release.JOURNAL).read_text())
            if journal["state"] == "rolled_back":
                break
            time.sleep(.1)
        else:
            stderr = self.watch_err.read_text() if self.watch_err.exists() else ""
            self.fail("external rollback did not complete: " + stderr[-2500:])
        self.assertEqual(self.live_plist.read_bytes(),
                         (self.root / "mvp-baseline.plist").read_bytes())
        self.assertEqual(self.launchctl("print", self.service_target).returncode, 0)
        self.assertFalse((self.state / release.FROZEN).exists())
        self.assertEqual((self.state / "supervisor-control.json").read_bytes(),
                         (self.root / "mvp-host-config.json").read_bytes())


if __name__ == "__main__":
    unittest.main()
