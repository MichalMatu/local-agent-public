"""Portable CLI health observation regressions; never touch installed agent."""
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

from local_agent.cli.diagnostics import build_parser, command_health
from local_agent.runtime.health_snapshot import observe_local_health

NOW = datetime(2026, 10, 16, 18, 0, tzinfo=timezone.utc)


class LocalHealthSnapshotTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="la-cli-health-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.status = self.root / "status.json"
        self.registry = self.root / "repositories.json"
        self.write(self.status, {
            "version": 1, "state": "running", "pid": 4242,
            "updated_at": NOW.isoformat(), "daemon_version": "4.20.6",
            "self_revision": "a" * 40, "current_task_id": None,
            "operator_secret": "private-do-not-expose",
        })
        self.write(self.registry, {
            "version": 1,
            "repositories": [
                {"id": "local-agent", "enabled": True, "repository": "private/one"},
                {"id": "hidden-repo", "enabled": False, "repository": "private/two"},
            ],
        })

    @staticmethod
    def write(path: Path, value: object) -> None:
        path.write_text(json.dumps(value) + "\n", encoding="utf-8")
        path.chmod(0o600)

    def observe(self, **kwargs):
        return observe_local_health(
            status_file=self.status, registry_file=self.registry,
            now=NOW, pid_alive=lambda pid: pid == 4242, **kwargs,
        )

    def test_idle_running_and_busy_snapshot_redacts_private_fields(self) -> None:
        result = self.observe()
        self.assertEqual(result["status"], "healthy")
        self.assertEqual(result["state"], "running")
        self.assertEqual(result["repository_ids"], ["local-agent"])
        self.assertEqual(result["repository_count"], 1)
        self.assertEqual(result["source_revision"], "a" * 40)
        self.assertEqual(result["execution_authority"], "not_asserted")
        self.assertNotIn("private-do-not-expose", json.dumps(result))
        self.assertNotIn(str(self.root), json.dumps(result))
        self.write(self.status, {
            "version": 1, "state": "idle", "pid": 4242,
            "updated_at": NOW.isoformat(),
        })
        self.assertEqual(self.observe()["state"], "idle")
        self.write(self.status, {
            "version": 1, "state": "running", "pid": 4242,
            "current_task_id": "task-1", "updated_at": NOW.isoformat(),
        })
        self.assertEqual(self.observe()["state"], "busy")

    def test_stale_and_dead_status_are_unverified_not_healthy(self) -> None:
        self.write(self.status, {
            "version": 1, "state": "running", "pid": 4242,
            "updated_at": (NOW - timedelta(seconds=181)).isoformat(),
        })
        self.assertIn("status_stale", self.observe()["warnings"])
        self.assertEqual(self.observe()["status"], "unverified")
        self.write(self.status, {
            "version": 1, "state": "running", "pid": 99,
            "updated_at": NOW.isoformat(),
        })
        self.assertIn("status_owner_not_running", self.observe()["warnings"])
        self.write(self.status, {
            "version": 1, "state": "running", "pid": True,
            "updated_at": NOW.isoformat(),
        })
        self.assertIn("status_owner_unknown", self.observe()["warnings"])

    def test_disabled_is_observed_but_not_healthy(self) -> None:
        self.write(self.status, {
            "version": 1, "state": "disabled", "pid": 4242,
            "updated_at": NOW.isoformat(),
        })
        self.assertEqual(self.observe()["status"], "degraded")
        self.assertEqual(self.observe()["state"], "disabled")

    def test_timestamp_schema_and_registry_fail_closed(self) -> None:
        self.write(self.status, {
            "version": 1, "state": "running", "pid": 4242,
            "updated_at": "not-a-timestamp",
        })
        self.assertIn("status_timestamp_invalid", self.observe()["warnings"])
        self.write(self.registry, {
            "version": 1, "repositories": [{"id": "foo"}, {"id": "FOO"}],
        })
        self.assertIn("registry_unavailable", self.observe()["warnings"])
        self.assertIsNone(self.observe()["repository_count"])

    def test_rejects_symlinks_world_writable_files_and_duplicate_json(self) -> None:
        self.status.unlink()
        self.status.symlink_to(self.registry)
        self.assertIn("status_unavailable", self.observe()["warnings"])
        self.status.unlink()
        self.write(self.status, {
            "version": 1, "state": "idle", "pid": 4242,
            "updated_at": NOW.isoformat(),
        })
        self.status.chmod(0o666)
        self.assertIn("status_unavailable", self.observe()["warnings"])
        self.status.write_text(
            '{"version":1,"version":1,"state":"idle"}', encoding="utf-8",
        )
        self.status.chmod(0o600)
        self.assertIn("status_unavailable", self.observe()["warnings"])

    def test_oversized_nonregular_and_relative_sources_are_rejected(self) -> None:
        self.status.write_text(" " * (32 * 1024 + 1), encoding="utf-8")
        self.assertIn("status_unavailable", self.observe()["warnings"])
        self.status.unlink()
        self.status.mkdir()
        self.assertIn("status_unavailable", self.observe()["warnings"])
        self.assertIn("status_unavailable", observe_local_health(
            status_file=Path("relative.json"), registry_file=self.registry,
            now=NOW,
        )["warnings"])

    def test_invalid_max_age_is_rejected_without_filesystem_access(self) -> None:
        for value in (0, 3601, True, -1):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    self.observe(max_age_seconds=value)

    def test_cli_health_parses_and_reports_consistent_exit_codes(self) -> None:
        parser = build_parser()
        args = parser.parse_args([
            "health", "--status-file", str(self.status),
            "--registry-file", str(self.registry), "--max-age-seconds", "3600",
        ])
        # CLI observation uses the current clock, not fixture's fixed date.
        self.write(self.status, {
            "version": 1, "state": "running", "pid": os.getpid(),
            "updated_at": datetime.now(timezone.utc).isoformat(),
        })
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = command_health(args)
        payload = json.loads(output.getvalue())
        self.assertEqual(code, 0)
        self.assertEqual(payload["status"], "healthy")
        self.assertNotIn(str(self.status), output.getvalue())
        self.assertNotIn("private/one", output.getvalue())
        self.status.unlink()
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = command_health(args)
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(output.getvalue())["status"], "unverified")

    def test_cli_does_not_use_network_or_mutate_inputs(self) -> None:
        before = (self.status.read_bytes(), self.registry.read_bytes())
        args = build_parser().parse_args([
            "health", "--status-file", str(self.status),
            "--registry-file", str(self.registry),
        ])
        with mock.patch("local_agent.cli.diagnostics.agentd.multirepo_registry_path",
                        side_effect=AssertionError("implicit registry access")), \
             mock.patch("local_agent.cli.diagnostics.core.publish_result",
                        side_effect=AssertionError("publication forbidden")):
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(command_health(args), 2)  # historical test timestamp
        self.assertEqual(before, (self.status.read_bytes(), self.registry.read_bytes()))


    def test_verified_supervisor_worker_count_is_advisory_not_free_capacity(self) -> None:
        self.write(self.status, {
            "version": 1, "state": "running", "pid": 4242,
            "updated_at": NOW.isoformat(), "active_workers_observed": True, "execution_model": "parallel_repository_supervisor", "supervisor_pid": 4242,
            "active_worker_count": 2, "max_parallel_workers": 4,
            "active_repository_ids": ["private-task-name"],
        })
        report = self.observe()
        self.assertEqual(report["status"], "healthy")
        self.assertTrue(report["active_workers_observed"])
        self.assertEqual(report["active_worker_count"], 2)
        self.assertEqual(report["max_parallel_workers_observed"], 4)
        self.assertNotIn("free_workers", report)
        self.assertNotIn("private-task-name", json.dumps(report))

    def test_generic_daemon_zero_is_not_verified_idle_workers(self) -> None:
        self.write(self.status, {
            "version": 1, "state": "idle", "pid": 4242,
            "updated_at": NOW.isoformat(), "active_workers_observed": False,
            "active_worker_count": None, "max_parallel_workers": 4,
        })
        report = self.observe()
        self.assertEqual(report["status"], "healthy")
        self.assertFalse(report["active_workers_observed"])
        self.assertIsNone(report["active_worker_count"])
        self.assertIsNone(report["max_parallel_workers_observed"])

    def test_invalid_worker_sample_fails_closed(self) -> None:
        for count, limit in ((5, 4), (True, 4), (-1, 4), (0, True), (0, 0)):
            with self.subTest(count=count, limit=limit):
                self.write(self.status, {
                    "version": 1, "state": "running", "pid": 4242,
                    "updated_at": NOW.isoformat(), "active_workers_observed": True, "execution_model": "parallel_repository_supervisor", "supervisor_pid": 4242,
                    "active_worker_count": count, "max_parallel_workers": limit,
                })
                report = self.observe()
                self.assertEqual(report["status"], "unverified")
                self.assertIn("worker_sample_invalid", report["warnings"])
                self.assertFalse(report["active_workers_observed"])
                self.assertIsNone(report["active_worker_count"])

    def test_worker_observation_stale_dead_or_missing_is_unknown(self) -> None:
        for stamp, pid in (
            (NOW - timedelta(seconds=181), 4242),
            (NOW, 88),
        ):
            with self.subTest(stamp=stamp, pid=pid):
                self.write(self.status, {
                    "version": 1, "state": "running", "pid": pid,
                    "updated_at": stamp.isoformat(), "active_workers_observed": True, "execution_model": "parallel_repository_supervisor", "supervisor_pid": pid,
                    "active_worker_count": 0, "max_parallel_workers": 4,
                })
                report = self.observe()
                self.assertEqual(report["status"], "unverified")
                self.assertFalse(report["active_workers_observed"])
                self.assertIsNone(report["active_worker_count"])


    def test_supervisor_identity_must_be_live_and_correct_model(self) -> None:
        for model, supervisor_pid, warning in (
            ("parallel_repository_supervisor", 99, "worker_observer_not_running"),
            ("serial", 4242, "worker_sample_invalid"),
        ):
            with self.subTest(model=model, supervisor_pid=supervisor_pid):
                self.write(self.status, {
                    "version": 1, "state": "running", "pid": 4242,
                    "updated_at": NOW.isoformat(), "active_workers_observed": True,
                    "execution_model": model, "supervisor_pid": supervisor_pid,
                    "active_worker_count": 0, "max_parallel_workers": 4,
                })
                report = self.observe()
                self.assertEqual(report["status"], "unverified")
                self.assertIn(warning, report["warnings"])
                self.assertFalse(report["active_workers_observed"])
                self.assertIsNone(report["active_worker_count"])


if __name__ == "__main__":
    unittest.main()
