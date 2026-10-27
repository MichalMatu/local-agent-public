from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

from scripts import isolated_rollout_controller as transaction
from scripts import isolated_rollout_observation as observation
from scripts import isolated_rollout_watchdog as external_watchdog


class ObservationTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="la-rollout-observation-")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.source = self.root / "source"
        self.source.mkdir()
        self.status = self.root / "status.json"
        self.tasks = self.root / "tasks"
        self.results = self.root / "results"
        self.claims = self.root / "claims"
        self.spool = self.root / "spool"
        self.hclaims = self.root / "hclaims"
        self.hresults = self.root / "hresults"
        for path in (self.tasks, self.results, self.claims, self.spool, self.hclaims, self.hresults):
            path.mkdir()
        self.set_status()
        self.kwargs = dict(task_dir=self.tasks, result_dir=self.results,
                           claim_dir=self.claims, spool_dir=self.spool,
                           host_claim_dir=self.hclaims, host_result_dir=self.hresults)

    def set_status(self, **overrides):
        fields = dict(
            version=1, self_revision=transaction.BASE_SHA, state="idle",
            current_task_id=None, current_attempt_id=None,
            active_repository_ids=[], active_host_task_ids=[],
            updated_at=dt.datetime.now(dt.timezone.utc).isoformat(),
        )
        fields.update(overrides)
        self.status.write_text(json.dumps(fields))

    def test_fresh_idle_status(self):
        self.assertEqual(observation.observe_local_status(self.status)["state"], "idle")

    def test_stale_running_and_duplicate_status_fail_closed(self):
        self.set_status(state="running")
        with self.assertRaises(transaction.RolloutRejected):
            observation.observe_local_status(self.status)
        self.set_status(updated_at="2020-01-01T00:00:00+00:00")
        with self.assertRaises(transaction.RolloutRejected):
            observation.observe_local_status(self.status)
        self.status.write_text('{"version":1,"version":1}')
        with self.assertRaisesRegex(transaction.RolloutRejected, "duplicate"):
            observation.observe_local_status(self.status)

    def test_hidden_and_symlink_queue_entries(self):
        (self.tasks / ".gitkeep").write_text("")
        self.assertEqual(observation.observe_queue(**self.kwargs)["pending_tasks"], 0)
        (self.tasks / "task.json").symlink_to(self.status)
        with self.assertRaisesRegex(transaction.RolloutRejected, "nonregular"):
            observation.observe_queue(**self.kwargs)

    def test_pending_and_claims_are_not_quiescent(self):
        (self.tasks / "task.json").write_text("{}")
        self.assertEqual(observation.observe_queue(**self.kwargs)["pending_tasks"], 1)
        (self.results / "task.json").write_text('{"status":"done"}')
        self.assertTrue(observation.observe_queue(**self.kwargs)["quiescent_observation"])
        (self.claims / "claim.json").write_text("{}")
        self.assertFalse(observation.observe_queue(**self.kwargs)["quiescent_observation"])

    def test_unknown_is_never_treated_as_clear(self):
        (self.results / "past.json").write_text('{"status":"unknown"}')
        self.assertEqual(observation.observe_queue(**self.kwargs)["unreconciled_results"], 1)
        self.assertFalse(observation.observe_queue(**self.kwargs)["quiescent_observation"])
        (self.results / "past.json").write_text(
            '{"status":"failed","failure_reason":"interrupted_execution"}'
        )
        self.assertFalse(observation.observe_queue(**self.kwargs)["quiescent_observation"])

    def test_host_unreconciled_claim(self):
        (self.hclaims / "once.json").write_text("{}")
        self.assertEqual(observation.observe_queue(**self.kwargs)["host_unreconciled_claims"], 1)
        (self.hresults / "once.json").write_text('{"status":"unknown"}')
        self.assertEqual(observation.observe_queue(**self.kwargs)["host_unreconciled_claims"], 0)
        # This local host UNKNOWN result is still a manual reconciliation
        # gate in the eventual installer, which does not exist yet.

    def test_host_terminal_unknown_blocks_quiescent_observation(self):
        (self.hresults / "once.json").write_text('{"status":"unknown"}')
        result = observation.observe_queue(**self.kwargs)
        self.assertEqual(result["unreconciled_results"], 1)
        self.assertFalse(result["quiescent_observation"])

    def test_supervisor_identity_change_breaks_double_read(self):
        proof = {"source_sha": transaction.BASE_SHA, "source_path": str(self.source)}
        with mock.patch.object(observation, "source_proof", return_value=proof), \
             mock.patch.object(observation, "observe_local_status", side_effect=[
                 {"supervisor_pid": 1, "state": "idle"},
                 {"supervisor_pid": 2, "state": "idle"},
             ]):
            receipt = observation.observe_twice(
                source=self.source, status=self.status,
                mailboxes=((self.tasks, self.results),),
                claim_dir=self.claims, spool_dir=self.spool,
                expected_mailboxes=1,
                host_claim_dir=self.hclaims, host_result_dir=self.hresults,
            )
        self.assertFalse(receipt["double_read_stable"])
        self.assertFalse(receipt["quiescent_observation"])

    def test_source_provenance_requires_exact_sha_and_clean(self):
        with mock.patch.object(observation, "_git", side_effect=[transaction.BASE_SHA, ""]):
            self.assertEqual(observation.source_proof(self.source)["source_sha"], transaction.BASE_SHA)
        with mock.patch.object(observation, "_git", return_value="a" * 40):
            with self.assertRaisesRegex(transaction.RolloutRejected, "accepted installed base"):
                observation.source_proof(self.source)
        with mock.patch.object(observation, "_git", side_effect=[transaction.BASE_SHA, " M important.py"]):
            with self.assertRaisesRegex(transaction.RolloutRejected, "not clean"):
                observation.source_proof(self.source)

    def test_double_observation_never_authorizes_a_switch(self):
        with mock.patch.object(observation, "source_proof",
                               return_value={"source_sha": transaction.BASE_SHA,
                                             "source_path": str(self.source)}):
            receipt = observation.observe_twice(
                source=self.source, status=self.status,
                mailboxes=((self.tasks, self.results),),
                claim_dir=self.claims, spool_dir=self.spool,
                expected_mailboxes=1,
                host_claim_dir=self.hclaims, host_result_dir=self.hresults,
            )
        self.assertTrue(receipt["quiescent_observation"])
        self.assertFalse(receipt["switch_authorized"])
        self.assertTrue(receipt["requires_exclusive_admission_fence"])
        self.assertEqual(len(receipt["evidence_digest"]), 64)

    def test_incomplete_mailbox_inventory_fails_closed(self):
        with self.assertRaisesRegex(transaction.RolloutRejected, "inventory"):
            observation.observe_twice(
                source=self.source, status=self.status,
                mailboxes=((self.tasks, self.results),),
                claim_dir=self.claims, spool_dir=self.spool,
                expected_mailboxes=2,
            )

    def test_missing_queue_directory_fails_closed(self):
        shutil.rmtree(self.spool)
        with self.assertRaisesRegex(transaction.RolloutRejected, "missing"):
            observation.observe_queue(**self.kwargs)


class IndependentWatchdogTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="la-rollout-watchdog-test-")
        self.addCleanup(tmp.cleanup)
        self.parent = Path(tmp.name)
        self.root = self.parent / "la-rollout-external"
        self.root.mkdir()
        (self.root / transaction.MARKER).write_text(transaction.MARKER_VALUE)
        baseline = self.parent / "base"
        candidate = self.parent / "candidate"
        baseline.mkdir()
        for relative in transaction.CHANGED:
            path = baseline / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("old")
        shutil.copytree(baseline, candidate)
        for relative in transaction.CHANGED | transaction.ADDED:
            path = candidate / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("new")
        transaction.prepare(self.root, baseline, candidate)
        self.before = dict(daemon_state="idle", active_tasks=0, pending_tasks=0,
                           unreconciled_unknown=0, source_sha=transaction.BASE_SHA)

    def test_external_watchdog_rolls_back_unaccepted_pointer(self):
        transaction.arm(self.root, self.before, watchdog_seconds=1)
        process = subprocess.Popen(
            [sys.executable, "-m", "scripts.isolated_rollout_watchdog", str(self.root)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        out, err = process.communicate(timeout=8)
        self.assertEqual(process.returncode, 0, err)
        self.assertIn("ROLLED_BACK", out)
        self.assertEqual(transaction._pointer(self.root), "snapshots/baseline")

    def test_external_watchdog_respects_explicit_acceptance(self):
        transaction.arm(self.root, self.before, watchdog_seconds=3)
        process = subprocess.Popen(
            [sys.executable, "-m", "scripts.isolated_rollout_watchdog", str(self.root)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        healthy = dict(service_active=True, heartbeat_fresh=True,
                       source_digest_ok=True, queues_safe=True)
        transaction.watchdog(self.root, healthy)
        out, err = process.communicate(timeout=8)
        self.assertEqual(process.returncode, 0, err)
        self.assertIn("ACCEPTED", out)
        self.assertEqual(transaction._pointer(self.root), "snapshots/candidate")

    def test_watchdog_outlives_short_lived_launcher(self):
        transaction.arm(self.root, self.before, watchdog_seconds=1)
        cmd = (
            "import subprocess,sys;"
            "subprocess.Popen([sys.executable,'-m',"
            "'scripts.isolated_rollout_watchdog',sys.argv[1]],"
            "stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,"
            "stderr=subprocess.DEVNULL,start_new_session=True)"
        )
        subprocess.run([sys.executable, "-c", cmd, str(self.root)],
                       check=True, timeout=5)
        until = time.monotonic() + 7
        while time.monotonic() < until:
            if transaction._journal(self.root)["state"] == "rolled_back":
                break
            time.sleep(0.1)
        self.assertEqual(transaction._journal(self.root)["state"], "rolled_back")
        self.assertEqual(transaction._pointer(self.root), "snapshots/baseline")

    def test_rejects_unarmed_watchdog(self):
        with self.assertRaisesRegex(transaction.RolloutRejected, "not independently armed"):
            external_watchdog.run_until_deadline(self.root)


if __name__ == "__main__":
    unittest.main()
