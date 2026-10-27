"""Opt-in physical Debian test of independent exact-SHA user-service rollback.

No persistent service file, no enable/linger, no real GitHub and no live pilot
identity/config/transport. Two clean separate Git clones are disposable.
"""
from __future__ import annotations

# ruff: noqa: E402 -- local source import setup in standalone test

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from linux_agent.release_controller import (
    DisposableUserServiceController, ServiceSwitchError,
)
from linux_agent.service_plan import ReleasePair



def _cmd(*args: str) -> str:
    return subprocess.run(args, capture_output=True, text=True, check=True,
                          timeout=90).stdout.strip()


@unittest.skipUnless(os.environ.get("LOCAL_AGENT_NATIVE_SYSTEMD") == "1",
                     "opt-in only: disposable physical Debian user service")
class NativeReleaseControllerTests(unittest.TestCase):
    def test_exact_sha_candidate_and_real_external_rollback(self):
        project = ROOT.parents[2]
        candidate_sha = _cmd("git", "-C", str(project), "rev-parse", "HEAD")
        with tempfile.TemporaryDirectory(prefix="linux-agent-release-") as temp:
            root = Path(temp)
            releases = root / "releases"
            releases.mkdir()
            # Test-only synthetic baseline in a disposable clone. It proves
            # controller source switching, not an accepted production baseline.
            seed = root / "baseline-fixture"
            _cmd("git", "clone", "--quiet", "--no-hardlinks", str(project), str(seed))
            _cmd("git", "-C", str(seed), "-c", "user.name=Fixture",
                 "-c", "user.email=fixture@local.invalid", "commit", "--allow-empty",
                 "-m", "test-only rollback baseline")
            prior_source = _cmd("git", "-C", str(seed), "rev-parse", "HEAD")
            self.assertNotEqual(candidate_sha, prior_source)
            # Avoid shared Git object files or code/runtime imports to the
            # Mac checkout: each version is a clean standalone source clone.
            for sha in (candidate_sha, prior_source):
                checkout = releases / sha
                _cmd("git", "clone", "--quiet", "--local", "--no-hardlinks",
                     str(seed), str(checkout))
                _cmd("git", "-C", str(checkout), "checkout", "--quiet",
                     "--detach", sha)
            bare = root / "offline-control.git"
            _cmd("git", "init", "--bare", str(bare))
            writer = root / "offline-publisher"
            _cmd("git", "clone", "--quiet", str(bare), str(writer))
            _cmd("git", "-C", str(writer),
                 "-c", "user.name=Fixture", "-c", "user.email=fixture@local.invalid",
                 "commit", "--allow-empty", "-m", "init")
            _cmd("git", "-C", str(writer), "branch", "-M", "agent-control")
            _cmd("git", "-C", str(writer), "push", "-u", "origin", "agent-control")
            config = root / "config.json"
            config.write_text(json.dumps({
                "version": 1, "host_id": "release-lab-" + uuid.uuid4().hex[:8],
                "installation_id": str(uuid.uuid4()),
                "repository": str(bare), "branch": "agent-control",
                "enabled": True,
                "host_intake": {"enabled": True, "authorization": "github_writers"},
            }))
            config.chmod(0o600)
            state = root / "private-state"
            state.mkdir(mode=0o700)
            pair = ReleasePair(
                releases / candidate_sha, candidate_sha,
                releases / prior_source, prior_source,
                Path(sys.executable).resolve(), config, state,
            )
            name = "local-agent-linux-lab-" + uuid.uuid4().hex[:16] + ".service"
            controller = DisposableUserServiceController(pair, name)
            try:
                baseline_pid = controller.stage_baseline()
                self.assertGreater(baseline_pid, 1)
                candidate_pid = controller.switch_to_candidate()
                self.assertNotEqual(candidate_pid, baseline_pid)
                rollback_pid = controller.rollback_candidate()
                self.assertGreater(rollback_pid, 1)
                self.assertEqual(
                    controller.unit_file.read_text(),
                    pair.unit_for(pair.rollback, pair.rollback_sha),
                )
                # Exercise automatic rollback after candidate did start, but
                # acceptance validation reports an injected failure.
                original_start = controller._start

                def fault_once(release: Path) -> int:
                    pid = original_start(release)
                    if release == pair.candidate:
                        raise ServiceSwitchError("injected after-start acceptance failure")
                    return pid

                controller._start = fault_once
                with self.assertRaisesRegex(ServiceSwitchError, "rollback restored"):
                    controller.switch_to_candidate()
                controller._start = original_start
                self.assertEqual(
                    controller.unit_file.read_text(),
                    pair.unit_for(pair.rollback, pair.rollback_sha),
                )
                controller._check_active(pair.rollback)
                # Exercise a real *external* systemd service restart after
                # rollback. The same accepted SHA must come back under a
                # new MainPID and the controller must still identify it.
                old_pid = controller._check_active(pair.rollback)
                _cmd("systemctl", "--user", "restart", name)
                new_pid = controller._check_active(pair.rollback)
                self.assertNotEqual(new_pid, old_pid)
            finally:
                # A failed baseline start may already have created a unit.
                # Its durable "baseline_starting" journal requires external
                # recovery before safe cleanup; never orphan a lab service.
                if controller.transaction_path.exists():
                    phase = controller._journal()["phase"]
                    if phase in {
                        "baseline_starting", "candidate_requested",
                        "candidate_unverified", "rollback_requested",
                    }:
                        controller.recover_unverified()
                    controller.cleanup_lab()
                    self.assertFalse(controller.unit_file.exists())
                    self.assertEqual(controller._journal()["phase"], "cleaned")


if __name__ == "__main__":
    unittest.main()
