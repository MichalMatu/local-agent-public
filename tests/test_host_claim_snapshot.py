"""Focused tests for host claim visibility without replay or lock probing."""
from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import local_agent.daemon.service as agentd
from local_agent.cli.diagnostics import build_parser
from local_agent.cli.host_claims import command_host_claims
from local_agent.runtime.host_claim_snapshot import MAX_RECORDS, observe_host_claims
from local_agent.supervisor import orchestrator


class HostClaimSnapshotTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="la-host-claim-census-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.root.chmod(0o700)

    def _folder(self, name: str) -> Path:
        base = self.root / "host-tasks"
        base.mkdir(mode=0o700, exist_ok=True)
        folder = base / name
        folder.mkdir(mode=0o700, exist_ok=True)
        return folder

    def _record(self, name: str, task_id: str) -> Path:
        path = self._folder(name) / f"{task_id}.json"
        path.write_text("private content", encoding="utf-8")
        path.chmod(0o600)
        return path

    def test_absent_and_empty_ledger_is_verified_zero(self) -> None:
        for stage in (0, 1, 2):
            if stage == 1:
                self._folder("claims")
            if stage == 2:
                self._folder("requests")
            with self.subTest(stage=stage):
                result = observe_host_claims(state_root=self.root)
                self.assertEqual(result["status"], "observed")
                self.assertEqual(result["claims_state"], "empty")
                self.assertEqual(result["claim_files"], 0)
                self.assertEqual(result["unresolved_claims"], 0)
                self.assertIsNone(result["restart_safe"])

    def test_history_is_not_live_work(self) -> None:
        self._record("claims", "completed")
        self._record("claims", "unresolved")
        self._record("results", "completed")
        self._record("requests", "completed")
        self._record("requests", "unresolved")
        self._record("requests", "not_claimed")
        self._record("remote-approvals", "completed")
        result = observe_host_claims(state_root=self.root)
        self.assertEqual(result["status"], "observed")
        self.assertEqual(result["claims_state"], "nonempty")
        self.assertEqual(result["claim_files"], 2)
        self.assertEqual(result["unresolved_claims"], 1)
        self.assertEqual(result["requests_without_result"], 2)
        self.assertEqual(result["result_files"], 1)
        self.assertEqual(result["remote_approval_files"], 1)
        self.assertFalse(result["active_workers_verified"])
        self.assertFalse(result["active_leases_verified"])
        self.assertIsNone(result["restart_safe"])
        self.assertNotIn("private content", json.dumps(result))
        self.assertNotIn(str(self.root), json.dumps(result))

    def test_readable_state_root_preserves_private_ledger_requirement(self) -> None:
        self.root.chmod(0o755)
        self._record("claims", "completed")
        self._record("results", "completed")
        result = observe_host_claims(state_root=self.root)
        self.assertEqual(result["status"], "observed")
        self.assertEqual(result["unresolved_claims"], 0)
        self.assertEqual(self.root.stat().st_mode & 0o777, 0o755)
        self._folder("claims").chmod(0o755)
        self.assertEqual(observe_host_claims(state_root=self.root)["status"], "unverified")
        self._folder("claims").chmod(0o700)
        self.root.chmod(0o775)
        self.assertEqual(observe_host_claims(state_root=self.root)["status"], "unverified")

    def test_symlink_and_unsafe_entries_fail_as_unknown(self) -> None:
        folder = self._folder("claims")
        (folder / "foreign.json").symlink_to(self.root / "nothing")
        result = observe_host_claims(state_root=self.root)
        self.assertEqual(result["status"], "unverified")
        self.assertEqual(result["claims_state"], "unknown")
        self.assertIsNone(result["claim_files"])
        (folder / "foreign.json").unlink()
        folder.chmod(0o777)
        self.assertEqual(observe_host_claims(state_root=self.root)["status"], "unverified")

    def test_missing_root_and_overflow_do_not_claim_empty(self) -> None:
        self.assertEqual(observe_host_claims(
            state_root=self.root / "absent",
        )["claims_state"], "unknown")
        self.assertEqual(observe_host_claims(
            state_root=Path("relative"),
        )["claims_state"], "unknown")
        folder = self._folder("results")
        for index in range(MAX_RECORDS + 1):
            path = folder / f"{index}.json"
            path.touch(mode=0o600)
        result = observe_host_claims(state_root=self.root)
        self.assertEqual(result["status"], "unverified")
        self.assertIsNone(result["result_files"])

    def test_cli_and_daemon_status_include_census_without_mutation(self) -> None:
        self._record("claims", "unresolved")
        args = build_parser().parse_args(["host-claims", "--state-root", str(self.root)])
        capture = io.StringIO()
        with contextlib.redirect_stdout(capture):
            self.assertEqual(command_host_claims(args), 0)
        self.assertEqual(json.loads(capture.getvalue())["unresolved_claims"], 1)
        with (
            mock.patch.object(agentd, "STATE_DIR", self.root),
            mock.patch.object(agentd, "self_revision", return_value="a" * 40),
        ):
            result = agentd.daemon_status_payload("idle")
        self.assertEqual(result["host_claims"]["claims_state"], "nonempty")
        self.assertFalse(result["active_workers_observed"])
        self.assertIsNone(result["active_worker_count"])
        self.assertIsNone(result["host_claims"]["restart_safe"])

    def test_local_supervisor_distinguishes_zero_from_active(self) -> None:
        state_file = self.root / "status.json"
        with (
            mock.patch.object(agentd, "STATE_DIR", self.root),
            mock.patch.object(agentd, "LOCAL_STATUS_PATH", state_file),
            mock.patch.object(agentd, "self_revision", return_value="a" * 40),
        ):
            orchestrator.publish_local_supervisor_status({}, max_workers=4)
            empty = json.loads(state_file.read_text())
            self.assertEqual(empty["active_worker_count"], 0)
            self.assertTrue(empty["active_workers_observed"])
            slot = orchestrator.RunningWorker(
                "repository-a", mock.Mock(), 1.0,
            )
            orchestrator.publish_local_supervisor_status(
                {"repository-a": slot}, max_workers=4,
            )
        active = json.loads(state_file.read_text())
        self.assertEqual(active["active_worker_count"], 1)
        self.assertEqual(active["active_repository_ids"], ["repository-a"])
        self.assertEqual(active["state"], "running")


if __name__ == "__main__":
    unittest.main()
