"""Phase B: one shared read-only Core overview for CLI and future GUI."""
from __future__ import annotations

import contextlib
import io
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from local_agent.cli.diagnostics import build_parser
from local_agent.cli.overview import command_overview
from local_agent.runtime.overview_snapshot import observe_local_overview

NOW = datetime(2026, 10, 17, 12, 0, tzinfo=timezone.utc)


class LocalOverviewTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="la-overview-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.state = self.root / "state"
        self.state.mkdir(mode=0o700)
        self.status = self.root / "status.json"
        self.registry = self.root / "repositories.json"
        self.write(self.status, {
            "version": 1, "state": "idle", "pid": os.getpid(),
            "updated_at": NOW.isoformat(), "self_revision": "a" * 40,
            "secret": "never-publish-me",
        })
        self.write(self.registry, {
            "version": 1, "repositories": [
                {"id": "first", "enabled": True, "repository": "private/secret"},
                {"id": "disabled", "enabled": False},
            ],
        })
        for kind in ("claims", "result-spool", "corrupt-claims"):
            (self.state / "repositories/first" / kind).mkdir(parents=True, mode=0o700)

    @staticmethod
    def write(path: Path, payload: dict[str, object]) -> None:
        path.write_text(json.dumps(payload) + "\n", encoding="utf-8")
        path.chmod(0o600)

    def snapshot(self, **kwargs) -> dict[str, object]:
        return observe_local_overview(
            status_file=self.status, registry_file=self.registry,
            state_root=self.state, now=NOW,
            pid_alive=lambda pid: pid == os.getpid(), **kwargs,
        )

    def test_green_empty_snapshot_is_readonly_and_redacted(self) -> None:
        original = self.registry.read_bytes(), self.status.read_bytes()
        response = self.snapshot()
        self.assertEqual(response["status"], "healthy")
        self.assertEqual(response["summary"]["repository_count"], 1)
        self.assertEqual(response["summary"]["record_counts"], {
            "claims": 0, "result-spool": 0, "corrupt-claims": 0,
        })
        self.assertFalse(response["summary"]["records_present"])
        self.assertFalse(response["consistent_point_in_time"])
        self.assertFalse(response["mutated"])
        self.assertEqual(response["execution_authority"], "not_asserted")
        self.assertEqual(response["replay_authority"], "not_asserted")
        self.assertNotIn("never-publish-me", json.dumps(response))
        self.assertNotIn("private/secret", json.dumps(response))
        self.assertNotIn(str(self.root), json.dumps(response))
        self.assertEqual((self.registry.read_bytes(), self.status.read_bytes()), original)

    def test_durable_records_are_attention_not_replay_authority(self) -> None:
        record = self.state / "repositories/first/claims/uncertain.json"
        record.write_text("private claim payload", encoding="utf-8")
        response = self.snapshot()
        self.assertEqual(response["status"], "attention")
        self.assertEqual(response["summary"]["record_counts"]["claims"], 1)
        self.assertTrue(response["summary"]["records_present"])
        self.assertNotIn("private claim payload", json.dumps(response))
        self.assertEqual(response["replay_authority"], "not_asserted")
        self.assertEqual(response["health"]["status"], "healthy")

    def test_missing_claims_stays_unverified_not_zero(self) -> None:
        (self.state / "repositories/first/claims").rmdir()
        response = self.snapshot()
        self.assertEqual(response["status"], "unverified")
        self.assertIsNone(response["summary"]["records_present"])
        self.assertIsNone(response["summary"]["record_counts"]["claims"])
        self.assertIn("backlog:claims_unavailable", response["warnings"])

    def test_stale_daemon_and_disabled_are_not_healthy(self) -> None:
        self.write(self.status, {
            "version": 1, "state": "idle", "pid": os.getpid(),
            "updated_at": (NOW - timedelta(minutes=15)).isoformat(),
        })
        response = self.snapshot()
        self.assertEqual(response["status"], "unverified")
        self.assertIn("health:status_stale", response["warnings"])
        self.write(self.status, {
            "version": 1, "state": "disabled", "pid": os.getpid(),
            "updated_at": NOW.isoformat(),
        })
        self.assertEqual(self.snapshot()["status"], "degraded")

    def test_zero_project_registry_requires_no_repository_directories(self) -> None:
        self.write(self.registry, {"version": 1, "repositories": []})
        response = self.snapshot()
        self.assertEqual(response["status"], "healthy")
        self.assertEqual(response["summary"]["repository_count"], 0)
        self.assertEqual(response["backlog"]["repositories"], [])
        self.assertEqual(response["summary"]["records_present"], False)

    def test_cli_codes_and_explicit_files_use_same_core_contract(self) -> None:
        parser = build_parser()
        args = parser.parse_args([
            "overview", "--status-file", str(self.status),
            "--registry-file", str(self.registry),
            "--state-root", str(self.state),
        ])
        self.write(self.status, {
            "version": 1, "state": "idle", "pid": os.getpid(),
            "updated_at": datetime.now(timezone.utc).isoformat(),
        })
        with contextlib.redirect_stdout(io.StringIO()) as out:
            code = command_overview(args)
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out.getvalue())["status"], "healthy")
        (self.state / "repositories/first/result-spool/pending.json").write_text("opaque")
        with contextlib.redirect_stdout(io.StringIO()) as out:
            code = command_overview(args)
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(out.getvalue())["status"], "attention")
        self.status.unlink()
        with contextlib.redirect_stdout(io.StringIO()) as out:
            code = command_overview(args)
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(out.getvalue())["status"], "unverified")

    def test_invalid_request_is_sanitized_and_has_no_effects(self) -> None:
        args = build_parser().parse_args([
            "overview", "--status-file", str(self.status),
            "--registry-file", str(self.registry),
            "--state-root", str(self.state),
            "--max-age-seconds", "0",
        ])
        before = self.registry.read_bytes(), self.status.read_bytes()
        with mock.patch(
            "local_agent.cli.diagnostics.core.publish_result",
            side_effect=AssertionError("must not publish"),
        ), mock.patch(
            "local_agent.cli.diagnostics.storage.sync_control",
            side_effect=AssertionError("must not touch Git"),
        ), contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(command_overview(args), 2)
        report = json.loads(out.getvalue())
        self.assertEqual(report["status"], "unverified")
        self.assertEqual(report["warnings"], ["invalid_overview_request"])
        self.assertFalse(report["mutated"])
        self.assertEqual(before, (self.registry.read_bytes(), self.status.read_bytes()))


    def test_opt_in_host_resources_preserves_default_overview_and_no_claims(self) -> None:
        self.write(self.status, {
            "version": 1, "state": "idle", "pid": os.getpid(),
            "updated_at": datetime.now(timezone.utc).isoformat(),
        })
        basic_args = build_parser().parse_args([
            "overview", "--status-file", str(self.status),
            "--registry-file", str(self.registry), "--state-root", str(self.state),
        ])
        with mock.patch(
            "local_agent.cli.overview.HostOverviewCollector",
            side_effect=AssertionError("default must not probe hardware"),
        ), contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(command_overview(basic_args), 0)
        self.assertNotIn("host_resources", json.loads(out.getvalue()))

        args = build_parser().parse_args([
            "overview", "--status-file", str(self.status),
            "--registry-file", str(self.registry), "--state-root", str(self.state),
            "--with-host-resources", "--resource-tools", "git", "node",
            "--resource-timeout", "8",
        ])
        data = {"contract_version": 1, "interpretation":
                "diagnostic_only_not_reserved_capacity", "profile": {},
                "capacity": {}, "tools": []}
        with mock.patch("local_agent.cli.overview.HostOverviewCollector") as collector, \
             contextlib.redirect_stdout(io.StringIO()) as out:
            collector.return_value.inspect.return_value = data
            self.assertEqual(command_overview(args), 0)
            self.assertEqual(collector.call_count, 1)
            limits = collector.return_value.inspect.call_args.kwargs["limits"]
            self.assertEqual(limits.timeout_seconds, 8.0)
            self.assertEqual(collector.return_value.inspect.call_args.kwargs["names"],
                             ("git", "node"))
        report = json.loads(out.getvalue())
        self.assertEqual(report["status"], "healthy")
        self.assertEqual(report["host_resources"], data)
        self.assertFalse(report["consistent_point_in_time"])
        self.assertEqual(report["execution_authority"], "not_asserted")

    def test_opt_in_host_resource_failure_is_unknown_not_ready(self) -> None:
        self.write(self.status, {
            "version": 1, "state": "idle", "pid": os.getpid(),
            "updated_at": datetime.now(timezone.utc).isoformat(),
        })
        args = build_parser().parse_args([
            "overview", "--status-file", str(self.status),
            "--registry-file", str(self.registry), "--state-root", str(self.state),
            "--with-host-resources",
        ])
        from local_agent.host_ops.capabilities.local.host import HostProfileError
        with mock.patch(
            "local_agent.cli.overview.HostOverviewCollector",
        ) as collector, contextlib.redirect_stdout(io.StringIO()) as out:
            collector.return_value.inspect.side_effect = HostProfileError(
                "private path /Users/sensitive"
            )
            self.assertEqual(command_overview(args), 2)
        report = json.loads(out.getvalue())
        self.assertEqual(report["status"], "unverified")
        self.assertEqual(report["host_resources"]["status"], "unverified")
        self.assertIn("host_resources_unavailable", report["warnings"])
        self.assertNotIn("sensitive", out.getvalue())
        self.assertFalse(report["consistent_point_in_time"])
        self.assertEqual(report["execution_authority"], "not_asserted")

    def test_optional_valid_resources_cannot_make_stale_core_healthy(self) -> None:
        args = build_parser().parse_args([
            "overview", "--status-file", str(self.status),
            "--registry-file", str(self.registry), "--state-root", str(self.state),
            "--with-host-resources",
        ])
        with mock.patch("local_agent.cli.overview.HostOverviewCollector") as collector, \
             contextlib.redirect_stdout(io.StringIO()) as out:
            collector.return_value.inspect.return_value = {
                "contract_version": 1, "profile": {}, "capacity": {}, "tools": [],
            }
            self.assertEqual(command_overview(args), 2)
        report = json.loads(out.getvalue())
        self.assertEqual(report["status"], "unverified")
        self.assertIn("health:status_stale", report["warnings"])


    def test_supervisor_worker_count_is_visible_but_not_a_free_slots_claim(self) -> None:
        self.write(self.status, {
            "version": 1, "state": "running", "pid": os.getpid(),
            "updated_at": NOW.isoformat(), "active_workers_observed": True, "execution_model": "parallel_repository_supervisor", "supervisor_pid": os.getpid(),
            "active_worker_count": 1, "max_parallel_workers": 4,
            "active_host_task_ids": ["private-task"],
        })
        report = self.snapshot()
        self.assertEqual(report["status"], "healthy")
        self.assertTrue(report["summary"]["active_workers_observed"])
        self.assertEqual(report["summary"]["active_worker_count"], 1)
        self.assertEqual(report["summary"]["max_parallel_workers_observed"], 4)
        self.assertNotIn("available_workers", report["summary"])
        self.assertNotIn("private-task", json.dumps(report))
        self.write(self.status, {
            "version": 1, "state": "running", "pid": os.getpid(),
            "updated_at": (NOW - timedelta(minutes=5)).isoformat(),
            "active_workers_observed": True, "execution_model": "parallel_repository_supervisor", "supervisor_pid": os.getpid(), "active_worker_count": 0,
            "max_parallel_workers": 4,
        })
        stale = self.snapshot()
        self.assertEqual(stale["status"], "unverified")
        self.assertFalse(stale["summary"]["active_workers_observed"])
        self.assertIsNone(stale["summary"]["active_worker_count"])


if __name__ == "__main__":
    unittest.main()
