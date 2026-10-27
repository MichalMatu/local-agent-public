"""Offline project lease/STOP admission regressions. No project task executed."""
from __future__ import annotations

import hashlib
import json
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
import tempfile
import unittest

from linux_agent.foundation.process import LEASE_FDS_ENV, LEASE_KEYS_DIGEST_ENV
from linux_agent.repository.admission import ProjectAdmissionGate, ProjectStopRequested
from linux_agent.repository.journal import ProjectJournal
from linux_agent.repository.mailbox import ProjectCandidate
from linux_agent.repository.projects import load_project_registry
from linux_agent.resources import MachineResourceBusy, resource_lock_dir

BINDING = "b0e94f93-478f-4fc6-abed-159d3702362f"
INSTALLATION = "9dfe2220-65ae-4624-b6db-8d9649fb2f8b"
HOST = "pi-zero2w"


class ProjectAdmissionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = self.enterContext(tempfile.TemporaryDirectory())
        self.home = Path(self.tmp)
        self.state = self.home / "private"
        registry = self.home / "registry.json"
        registry.write_text(json.dumps({"version": 1, "repositories": [{
            "id": "example-project", "repository": "example-owner/example-project",
            "agent_binding": BINDING, "host_id": HOST,
            "host_mailbox_enabled": True, "host_execution_enabled": True,
            "host_identity_file": str(self.home / "identity"),
            "control_dir": str(self.home / "control"),
            "work_dir": str(self.home / "work"),
            "checkpoints_dir": str(self.home / "checkpoints")
        }]}))
        (self.project,) = load_project_registry(
            registry, home=self.home, expected_host_id=HOST)
        self.journal = ProjectJournal(self.state, self.project, host_id=HOST,
                                      installation_id=INSTALLATION)
        self.gate = ProjectAdmissionGate(self.journal)

    def candidate(self, *, task_id="job1", **kwargs):
        task = {
            "id": task_id, "version": 1, "repository_id": "example-project",
            "agent_binding": BINDING, "target_host_id": HOST,
            "target_installation_id": INSTALLATION,
            "mode": "commands", "commands": ["printf fixture"], "resources": [],
        } | kwargs
        raw = json.dumps(task, sort_keys=True).encode()
        return ProjectCandidate(task_id, raw, hashlib.sha256(raw).hexdigest(),
                                task, "a" * 40)

    def test_lease_and_claim_are_durable_but_no_command_is_executed(self):
        candidate = self.candidate()
        before = {k: os.environ.get(k) for k in (LEASE_FDS_ENV, LEASE_KEYS_DIGEST_ENV)}
        with self.gate.admit(candidate) as active:
            self.assertEqual(active.task_digest, candidate.digest)
            self.assertEqual(active.resources, ())
            self.assertFalse(active.stop_requested())
            self.assertTrue(os.environ.get(LEASE_FDS_ENV))
            self.assertTrue(os.environ.get(LEASE_KEYS_DIGEST_ENV))
            self.assertTrue(self.journal._claim_path("job1").exists())
            self.assertEqual(self.journal.verified_results(), [])
        self.assertEqual({k: os.environ.get(k) for k in before}, before)
        with self.assertRaises(FileExistsError):
            with self.gate.admit(candidate):
                pass

    def test_machine_resource_is_shared_with_host_lock(self):
        first = self.candidate(task_id="a", resources=["machine"])
        other = self.candidate(task_id="b", resources=["camera"])
        with self.gate.admit(first):
            with self.assertRaises(MachineResourceBusy):
                with self.gate.admit(other):
                    pass
            self.assertFalse(self.journal._claim_path("b").exists())
        self.assertEqual(resource_lock_dir(self.state),
                         self.state / "locks" / "machine-resources")

    def test_named_resource_collision_rejected_before_claim(self):
        a = self.candidate(task_id="a", resources=["camera"])
        b = self.candidate(task_id="b", resources=["camera"])
        with self.gate.admit(a):
            with self.assertRaises(MachineResourceBusy):
                with self.gate.admit(b):
                    pass
        self.assertFalse(self.journal._claim_path("b").exists())

    def test_stop_refuses_admission_without_claim_and_is_idempotent(self):
        self.gate.request_stop()
        self.gate.request_stop()
        self.assertTrue(self.gate.stop_path.exists())
        with self.assertRaises(ProjectStopRequested):
            with self.gate.admit(self.candidate()):
                pass
        self.assertFalse(self.journal._claim_path("job1").exists())

    def test_stop_during_claim_is_visible_to_future_worker_poll(self):
        with self.gate.admit(self.candidate()) as active:
            self.gate.request_stop()
            self.assertTrue(active.stop_requested())
        self.assertTrue(self.journal._claim_path("job1").exists())

    def test_stop_ack_cannot_race_past_durable_claim(self):
        """Concurrent STOP must not acknowledge before a claim is fsynced."""
        original_claim = self.journal.claim
        stop_started = threading.Event()
        stop_ack = threading.Event()
        failures = []
        threads = []

        def stop_writer():
            stop_started.set()
            try:
                self.gate.request_stop()
                stop_ack.set()
            except BaseException as exc:
                failures.append(exc)

        def claim_with_stop_race(*args):
            thread = threading.Thread(target=stop_writer)
            threads.append(thread)
            thread.start()
            self.assertTrue(stop_started.wait(timeout=2))
            self.assertFalse(stop_ack.wait(timeout=0.05))
            return original_claim(*args)

        self.journal.claim = claim_with_stop_race
        try:
            with self.gate.admit(self.candidate()):
                self.assertTrue(self.journal._claim_path("job1").exists())
        finally:
            self.journal.claim = original_claim
            for thread in threads:
                thread.join(timeout=2)
        self.assertFalse(failures)
        self.assertTrue(stop_ack.is_set())
        self.assertTrue(self.gate.stop_path.is_file())
        with self.assertRaises(ProjectStopRequested):
            with self.gate.admit(self.candidate(task_id="job2")):
                pass
        self.assertFalse(self.journal._claim_path("job2").exists())

    def test_stop_symlink_fails_closed(self):
        self.journal._private_dir(self.journal.root, create=True)
        self.gate.stop_path.symlink_to(self.home / "unrelated")
        with self.assertRaisesRegex(ValueError, "symlink"):
            self.gate.request_stop()
        with self.assertRaisesRegex(ValueError, "symlink"):
            with self.gate.admit(self.candidate()):
                pass
        self.assertFalse(self.journal._claim_path("job1").exists())

    def test_missing_resource_declaration_refuses_before_claim(self):
        candidate = self.candidate()
        task = dict(candidate.task)
        task.pop("resources")
        raw = json.dumps(task).encode()
        corrupt = ProjectCandidate("job1", raw, hashlib.sha256(raw).hexdigest(),
                                   task, "a" * 40)
        with self.assertRaisesRegex(ValueError, "resources"):
            with self.gate.admit(corrupt):
                pass
        self.assertFalse(self.state.exists())

    def test_digest_mismatch_or_changed_candidate_refuses(self):
        valid = self.candidate()
        changed = ProjectCandidate(valid.task_id, valid.raw, "0" * 64,
                                   valid.task, valid.snapshot_sha)
        with self.assertRaisesRegex(ValueError, "digest"):
            with self.gate.admit(changed):
                pass
        changed2 = ProjectCandidate(valid.task_id, valid.raw, valid.digest,
                                    valid.task | {"id": "other"},
                                    valid.snapshot_sha)
        with self.assertRaisesRegex(ValueError, "changed"):
            with self.gate.admit(changed2):
                pass
        self.assertFalse(self.state.exists())

    def test_wrong_binding_or_host_fails_before_claim(self):
        for change in ({"agent_binding": "21010ac9-7eaa-47f2-9c58-180ac0a0819c"},
                       {"repository_id": "another"}, {"target_host_id": "other"},
                       {"target_installation_id": "other"}):
            with self.subTest(change=change):
                with self.assertRaises(ValueError):
                    with self.gate.admit(self.candidate(**change)):
                        pass
        self.assertFalse(self.state.exists())

    def test_interrupted_claim_becomes_unknown_only_after_lease_fence(self):
        candidate = self.candidate()
        with self.gate.admit(candidate):
            with self.assertRaises((BlockingIOError, RuntimeError, ValueError)):
                self.gate.recover_unknown_when_supervisor_stopped()
        self.assertEqual(self.gate.recover_unknown_when_supervisor_stopped(), 1)
        spool = self.journal.verified_results()
        self.assertEqual(len(spool), 1)
        self.assertEqual(json.loads(spool[0][1])["status"], "unknown")
        self.assertEqual(self.gate.recover_unknown_when_supervisor_stopped(), 0)

    @unittest.skipUnless(
        hasattr(os, "pidfd_open") and hasattr(signal, "pidfd_send_signal"),
        "requires Linux pidfd for verified orphan recovery",
    )
    def test_real_orphan_lease_holder_is_fenced_before_unknown(self):
        """Spawn ONLY a bounded test descendant holding inherited task FDs."""
        child = None
        try:
            with self.gate.admit(self.candidate()):
                fds = tuple(int(x) for x in os.environ[LEASE_FDS_ENV].split(","))
                ready = self.home / "test-child-ready"
                child = subprocess.Popen(
                    [
                        sys.executable, "-B", "-c",
                        "import pathlib,sys,time;"
                        "pathlib.Path(sys.argv[1]).write_text('ready');"
                        "time.sleep(30)",
                        str(ready),
                    ],
                    pass_fds=fds, stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    start_new_session=True,
                )
                until = time.monotonic() + 4
                while time.monotonic() < until and not ready.exists():
                    time.sleep(0.02)
                self.assertTrue(ready.exists(), "test descendant did not start")
            self.assertIsNone(child.poll())
            self.assertEqual(self.gate.recover_unknown_when_supervisor_stopped(), 1)
            child.wait(timeout=4)
            self.assertIsNotNone(child.returncode)
            result = json.loads(self.journal.verified_results()[0][1])
            self.assertEqual(result["status"], "unknown")
            self.assertEqual(result["result"]["reason"],
                             "interrupted_or_uncertain_project_execution_no_replay")
        finally:
            if child is not None and child.poll() is None:
                child.kill()
                child.wait(timeout=3)

    def test_missing_or_symlinked_lease_fails_closed_on_recovery(self):
        with self.gate.admit(self.candidate()):
            pass
        path = self.gate._lease_path("job1")
        path.unlink()
        with self.assertRaisesRegex(ValueError, "execution lease"):
            self.gate.recover_unknown_when_supervisor_stopped()
        self.assertEqual(self.journal.verified_results(), [])
        path.symlink_to(self.home / "unrelated")
        with self.assertRaisesRegex(ValueError, "execution lease"):
            self.gate.recover_unknown_when_supervisor_stopped()
        self.assertEqual(self.journal.verified_results(), [])


if __name__ == "__main__":
    unittest.main()
