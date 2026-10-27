from __future__ import annotations

import fcntl
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from local_agent.supervisor import macos_release_admission as gate


class MacReleaseAdmissionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="la-macos-admission-test-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        import local_agent.daemon.service as agentd
        state = mock.patch.object(agentd, "STATE_DIR", self.root)
        state.start()
        self.addCleanup(state.stop)
        self.config = self.root / gate.CONFIG
        self.marker = self.root / gate.FROZEN
        self.platform = mock.patch.object(gate.sys, "platform", "darwin")
        self.platform.start()
        self.addCleanup(self.platform.stop)
        # The parent task may be the live Mac service, which must require
        # admission. These isolated unit fixtures need the legacy no-opt-in
        # baseline; individual required-admission cases explicitly set "1".
        fixture_env = dict(os.environ)
        fixture_env.pop(gate.REQUIRED_ENV, None)
        env_patch = mock.patch.dict(os.environ, fixture_env, clear=True)
        env_patch.start()
        self.addCleanup(env_patch.stop)

    def configure(self, *, enabled=True):
        self.config.write_text(json.dumps({"version": 1, "enabled": enabled}) + "\n")
        self.config.chmod(0o600)

    def freeze(self):
        self.marker.write_text('{"state":"frozen"}\n')
        self.marker.chmod(0o600)

    def test_legacy_no_opt_in_remains_unchanged(self):
        with gate.macos_task_admission(self.root):
            self.assertFalse((self.root / gate.LOCK).exists())

    def test_disabled_config_preserves_existing_behavior(self):
        self.configure(enabled=False)
        with gate.macos_task_admission(self.root):
            self.assertFalse((self.root / gate.LOCK).exists())

    def test_required_enrollment_blocks_missing_and_disabled_config(self):
        with mock.patch.dict(os.environ, {gate.REQUIRED_ENV: "1"}):
            with self.assertRaisesRegex(gate.MacReleaseAdmissionBlocked, "not enrolled"):
                with gate.macos_task_admission(self.root):
                    self.fail("unenrolled required Mac process must not execute")
            self.configure(enabled=False)
            with self.assertRaisesRegex(gate.MacReleaseAdmissionBlocked, "disabled"):
                with gate.macos_task_admission(self.root):
                    self.fail("required Mac admission cannot be disabled")
            self.configure(enabled=True)
            with gate.macos_task_admission(self.root):
                self.assertTrue((self.root / gate.LOCK).exists())

    def test_invalid_required_enrollment_value_fails_closed(self):
        for invalid in ("true", "yes", "", "2"):
            with self.subTest(invalid=invalid):
                with mock.patch.dict(os.environ, {gate.REQUIRED_ENV: invalid}):
                    with self.assertRaisesRegex(
                        gate.MacReleaseAdmissionBlocked, "invalid Mac admission"
                    ):
                        with gate.macos_task_admission(self.root):
                            self.fail("invalid required flag must not execute")

    def test_required_enrollment_rejects_workers_before_task_effects(self):
        import local_agent.repository.worker as serial
        import local_agent.supervisor.worker as parallel
        import local_agent.supervisor.host_tasks as local
        import local_agent.supervisor.host_remote_worker as remote

        with mock.patch.dict(os.environ, {gate.REQUIRED_ENV: "1"}):
            with mock.patch.object(serial, "_poll_repository_once_admitted") as worker:
                with self.assertRaises(gate.MacReleaseAdmissionBlocked):
                    serial.poll_repository_once(mock.sentinel.repository)
                worker.assert_not_called()
            with mock.patch.object(parallel, "_poll_repository_once_admitted") as worker:
                with self.assertRaises(gate.MacReleaseAdmissionBlocked):
                    parallel.poll_repository_once(mock.sentinel.repository)
                worker.assert_not_called()
            with mock.patch.object(local, "_run_admitted_local_host_task") as worker:
                with self.assertRaises(gate.MacReleaseAdmissionBlocked):
                    local.run_local_host_task(
                        "x", state_dir=self.root, identity_file=self.root / "id",
                    )
                worker.assert_not_called()
            with mock.patch.object(remote, "_run_remote_host_task_admitted") as worker:
                with self.assertRaises(gate.MacReleaseAdmissionBlocked):
                    remote.run_remote_host_task_once(
                        "x", state_dir=self.root,
                        operator_checkout=self.root, production_checkout=self.root,
                    )
                worker.assert_not_called()

    def test_private_opt_in_lease_covers_entire_turn(self):
        self.configure()
        with gate.macos_task_admission(self.root):
            lock = self.root / gate.LOCK
            self.assertTrue(lock.is_file())
            subprocess_args = [
                sys.executable, "-c",
                "import fcntl,os,sys;h=os.open(sys.argv[1],os.O_RDWR);"
                "fcntl.flock(h,fcntl.LOCK_EX|fcntl.LOCK_NB)",
                str(lock),
            ]
            competing = subprocess.run(subprocess_args, capture_output=True, text=True, timeout=5)
            self.assertNotEqual(competing.returncode, 0)
        after = subprocess.run(subprocess_args, capture_output=True, text=True, timeout=5)
        self.assertEqual(after.returncode, 0, after.stderr)

    def test_durable_freeze_prevents_task_even_after_lock_release(self):
        self.configure()
        self.freeze()
        with self.assertRaisesRegex(gate.MacReleaseAdmissionBlocked, "frozen"):
            with gate.macos_task_admission(self.root):
                self.fail("task should not run")
        self.assertTrue(self.marker.is_file())

    def test_freeze_without_config_still_blocks(self):
        self.freeze()
        with self.assertRaisesRegex(gate.MacReleaseAdmissionBlocked, "orphaned"):
            with gate.macos_task_admission(self.root):
                self.fail("task should not run")

    def test_disabling_opt_in_cannot_clear_frozen_state(self):
        self.configure(enabled=False)
        self.freeze()
        with self.assertRaisesRegex(gate.MacReleaseAdmissionBlocked, "cannot be disabled"):
            with gate.macos_task_admission(self.root):
                self.fail("task should not run")

    def test_lock_exclusive_holder_denies_task(self):
        self.configure()
        lock = self.root / gate.LOCK
        with lock.open("w+b") as handle:
            os.chmod(lock, 0o600)
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaisesRegex(gate.MacReleaseAdmissionBlocked, "exclusive fence"):
                with gate.macos_task_admission(self.root):
                    self.fail("task should not run")

    def test_untrusted_config_and_fence_fail_closed(self):
        self.configure()
        self.config.chmod(0o666)
        with self.assertRaisesRegex(gate.MacReleaseAdmissionBlocked, "not private"):
            with gate.macos_task_admission(self.root):
                self.fail("task should not run")
        self.configure()
        self.freeze()
        self.marker.chmod(0o644)
        with self.assertRaisesRegex(gate.MacReleaseAdmissionBlocked, "not private"):
            with gate.macos_task_admission(self.root):
                self.fail("task should not run")

    def test_duplicate_config_and_symlink_are_rejected(self):
        self.config.write_text('{"version":1,"enabled":true,"enabled":false}')
        self.config.chmod(0o600)
        with self.assertRaisesRegex(gate.MacReleaseAdmissionBlocked, "duplicate"):
            with gate.macos_task_admission(self.root):
                self.fail("task should not run")
        self.config.unlink()
        self.config.symlink_to(self.root / "outside")
        with self.assertRaisesRegex(gate.MacReleaseAdmissionBlocked, "symlink"):
            with gate.macos_task_admission(self.root):
                self.fail("task should not run")

    def test_non_macos_is_inert_even_with_config(self):
        self.configure()
        self.platform.stop()
        with mock.patch.object(gate.sys, "platform", "linux"):
            with gate.macos_task_admission(self.root):
                self.assertFalse((self.root / gate.LOCK).exists())
        self.platform.start()

    def test_serial_and_parallel_repository_turns_block_before_work(self):
        import local_agent.repository.worker as serial
        import local_agent.supervisor.worker as parallel
        self.configure()
        self.freeze()
        with mock.patch.object(serial, "_poll_repository_once_admitted") as s:
            with self.assertRaises(gate.MacReleaseAdmissionBlocked):
                serial.poll_repository_once(mock.sentinel.repository)
            s.assert_not_called()
        with mock.patch.object(parallel, "_poll_repository_once_admitted") as p:
            with self.assertRaises(gate.MacReleaseAdmissionBlocked):
                parallel.poll_repository_once(mock.sentinel.repository)
            p.assert_not_called()

    def test_host_paths_refuse_before_staging_or_claim(self):
        import local_agent.supervisor.host_tasks as local
        import local_agent.supervisor.host_remote_worker as remote
        self.configure()
        self.freeze()
        with mock.patch.object(local, "_run_admitted_local_host_task") as stage:
            with self.assertRaises(gate.MacReleaseAdmissionBlocked):
                local.run_local_host_task(
                    "pending", state_dir=self.root,
                    identity_file=self.root / "identity.json",
                )
            stage.assert_not_called()
        with mock.patch.object(remote, "_run_remote_host_task_admitted") as stage:
            with self.assertRaises(gate.MacReleaseAdmissionBlocked):
                remote.run_remote_host_task_once(
                    "pending", state_dir=self.root,
                    operator_checkout=self.root, production_checkout=self.root,
                )
            stage.assert_not_called()

    def test_frozen_all_paths_have_no_task_side_effects(self):
        import local_agent.repository.worker as serial
        import local_agent.supervisor.worker as parallel
        import local_agent.supervisor.host_tasks as local
        import local_agent.supervisor.host_remote_worker as remote
        self.configure()
        self.freeze()
        operations = [
            lambda: serial.poll_repository_once(None),
            lambda: parallel.poll_repository_once(None),
            lambda: local.run_local_host_task(
                "x", state_dir=self.root, identity_file=self.root / "identity",
            ),
            lambda: remote.run_remote_host_task_once(
                "x", state_dir=self.root, operator_checkout=self.root,
                production_checkout=self.root,
            ),
        ]
        for operation in operations:
            with self.subTest(operation=operation):
                with self.assertRaises(gate.MacReleaseAdmissionBlocked):
                    operation()
        self.assertEqual(sorted(x.name for x in self.root.iterdir()),
                         sorted([gate.CONFIG, gate.FROZEN, gate.LOCK]))


if __name__ == "__main__":
    unittest.main()
