"""Ensure every Mac command-producing entrypoint observes the opt-in freeze.

All tests use an isolated private temporary state root. No live service,
operator grants, project mailbox or Mac launchd state is modified.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import local_agent.daemon.service as agentd
from local_agent.development import operator_campaign
from local_agent.supervisor import conversation
from local_agent.supervisor import macos_release_admission as gate


class MacReleaseAdmissionCoverageTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="la-mac-admission-coverage-")
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.patch_state = mock.patch.object(agentd, "STATE_DIR", self.root)
        self.patch_state.start()
        self.addCleanup(self.patch_state.stop)
        self.patch_platform = mock.patch.object(gate.sys, "platform", "darwin")
        self.patch_platform.start()
        self.addCleanup(self.patch_platform.stop)
        self.config = self.root / gate.CONFIG
        self.marker = self.root / gate.FROZEN
        self.config.write_text('{"version":1,"enabled":true}\n')
        self.config.chmod(0o600)
        # Historical no-opt-in coverage is a disposable fixture, not the
        # installed launchd service. Required cases explicitly enroll it.
        fixture_env = dict(os.environ)
        fixture_env.pop(gate.REQUIRED_ENV, None)
        env_patch = mock.patch.dict(os.environ, fixture_env, clear=True)
        env_patch.start()
        self.addCleanup(env_patch.stop)

    def freeze(self):
        self.marker.write_text('{"state":"frozen"}\n')
        self.marker.chmod(0o600)

    def test_nested_mac_leases_do_not_drop_outer_task_protection(self):
        def exclusive_blocked() -> bool:
            result = subprocess.run([
                sys.executable, "-c",
                "import fcntl,os,sys;"
                "fd=os.open(sys.argv[1],os.O_RDWR);"
                "fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)",
                str(self.root / gate.LOCK),
            ], capture_output=True, text=True, timeout=5, check=False)
            return result.returncode != 0

        with gate.macos_task_admission(self.root):
            with gate.macos_task_admission(self.root):
                self.assertTrue(exclusive_blocked())
            # The remote-host worker can invoke a nested local host worker.
            # Releasing the inner lock must not drop the outer turn-wide gate.
            self.assertTrue(exclusive_blocked())

    def test_supervisor_launch_paths_are_quietly_deferred_when_frozen(self):
        from local_agent.supervisor import orchestrator
        self.freeze()
        with mock.patch.object(orchestrator, "_start_worker_admitted") as spawn_repo, \
             mock.patch.object(orchestrator, "_start_host_task_worker_admitted") as spawn_host:
            self.assertFalse(orchestrator.start_worker(
                mock.sentinel.repository, registry_path=None, running={},
                max_workers=2,
            ))
            self.assertFalse(orchestrator.start_host_task_worker(
                mock.sentinel.host, "task", running={}, max_workers=2,
            ))
            spawn_repo.assert_not_called()
            spawn_host.assert_not_called()

    def test_supervisor_launch_holds_lease_until_spawn_returns(self):
        from local_agent.supervisor import orchestrator

        def launch(*args, **kwargs):
            lock = self.root / gate.LOCK
            attempt = subprocess.run([
                sys.executable, "-c",
                "import fcntl,os,sys;"
                "fd=os.open(sys.argv[1],os.O_RDWR);"
                "fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)",
                str(lock),
            ], capture_output=True, text=True, timeout=5, check=False)
            self.assertNotEqual(attempt.returncode, 0)
            return True

        with mock.patch.object(orchestrator, "_start_worker_admitted", side_effect=launch):
            self.assertTrue(orchestrator.start_worker(
                mock.sentinel.repository, registry_path=None, running={},
                max_workers=2,
            ))
        with mock.patch.object(
            orchestrator, "_start_host_task_worker_admitted", side_effect=launch,
        ):
            self.assertTrue(orchestrator.start_host_task_worker(
                mock.sentinel.host, "task", running={}, max_workers=2,
            ))

    def test_supervisor_legacy_without_opt_in_remains_unchanged(self):
        from local_agent.supervisor import orchestrator
        self.config.unlink()
        with mock.patch.object(orchestrator, "_start_worker_admitted", return_value=True):
            self.assertTrue(orchestrator.start_worker(
                mock.sentinel.repository, registry_path=None, running={},
                max_workers=2,
            ))
        with mock.patch.object(
            orchestrator, "_start_host_task_worker_admitted", return_value=True,
        ):
            self.assertTrue(orchestrator.start_host_task_worker(
                mock.sentinel.host, "task", running={}, max_workers=2,
            ))
        self.assertFalse((self.root / gate.LOCK).exists())

    def test_legacy_direct_daemon_claim_blocked_before_effects(self):
        self.freeze()
        with mock.patch.object(agentd, "_execute_task_admitted") as admitted:
            with self.assertRaises(gate.MacReleaseAdmissionBlocked):
                agentd.execute_task({"id": "never-claimed"})
            admitted.assert_not_called()

    def test_legacy_exec_shared_lease_covers_entire_task_effect(self):
        def direct_exec(*args, **kwargs):
            lock = self.root / gate.LOCK
            result = subprocess.run([
                sys.executable, "-c",
                "import fcntl,os,sys;"
                "fd=os.open(sys.argv[1],os.O_RDWR);"
                "fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)",
                str(lock),
            ], capture_output=True, text=True, timeout=5, check=False)
            self.assertNotEqual(result.returncode, 0)
            return "processed"

        with mock.patch.object(agentd, "_execute_task_admitted", side_effect=direct_exec):
            self.assertEqual(agentd.execute_task({"id": "held"}), "processed")

    def test_conversation_reservation_blocked_before_side_effects(self):
        self.freeze()
        with mock.patch.object(conversation, "_start_if_pending_admitted") as reserve:
            with self.assertRaises(gate.MacReleaseAdmissionBlocked):
                conversation.start_if_pending()
            reserve.assert_not_called()

    def test_conversation_child_browser_effects_blocked(self):
        self.freeze()
        with mock.patch.object(operator_campaign, "_run_operator_campaign_admitted") as run:
            with self.assertRaises(gate.MacReleaseAdmissionBlocked):
                operator_campaign.run_operator_campaign(
                    request_path=self.root / "req.json",
                    result_path=self.root / "result.json",
                    home=self.root,
                    root=self.root / "lab",
                    checkout=self.root / "isolated",
                    production_checkout=self.root / "production",
                )
            run.assert_not_called()

    def test_no_opt_in_does_not_change_legacy_or_conversation_paths(self):
        self.config.unlink()
        with mock.patch.object(agentd, "_execute_task_admitted", return_value="unchanged") as admitted:
            self.assertEqual(agentd.execute_task({}), "unchanged")
            admitted.assert_called_once()
        with mock.patch.object(conversation, "_start_if_pending_admitted", return_value=None):
            self.assertIsNone(conversation.start_if_pending())
        with mock.patch.object(operator_campaign, "_run_operator_campaign_admitted",
                               return_value={"status": "done"}):
            self.assertEqual(operator_campaign.run_operator_campaign(
                request_path=self.root / "req.json", result_path=self.root / "res.json",
                home=self.root, root=self.root / "lab",
                checkout=self.root / "checkout",
                production_checkout=self.root / "production",
            )["status"], "done")

    def test_orphaned_marker_blocks_every_newly_guarded_entrypoint(self):
        self.config.unlink()
        self.freeze()
        with self.assertRaisesRegex(gate.MacReleaseAdmissionBlocked, "orphaned"):
            agentd.execute_task({})
        with self.assertRaisesRegex(gate.MacReleaseAdmissionBlocked, "orphaned"):
            conversation.start_if_pending()
        with self.assertRaisesRegex(gate.MacReleaseAdmissionBlocked, "orphaned"):
            operator_campaign.run_operator_campaign(
                request_path=self.root / "req.json", result_path=self.root / "result.json",
                home=self.root, root=self.root, checkout=self.root,
                production_checkout=self.root,
            )

    def test_active_freeze_retains_unknown_no_replay_state(self):
        self.freeze()
        for path in ("claims/preexisting.json", "host-tasks/claims/unknown.json"):
            claim = self.root / path
            claim.parent.mkdir(parents=True, exist_ok=True)
            claim.write_text(json.dumps({"status": "unknown"}) + "\n")
        with self.assertRaises(gate.MacReleaseAdmissionBlocked):
            agentd.execute_task({})
        self.assertEqual((self.root / "claims/preexisting.json").read_text().strip(),
                         '{"status": "unknown"}')
        self.assertEqual((self.root / "host-tasks/claims/unknown.json").read_text().strip(),
                         '{"status": "unknown"}')


if __name__ == "__main__":
    unittest.main()
