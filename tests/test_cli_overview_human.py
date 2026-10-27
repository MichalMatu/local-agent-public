"""The optional human overview must not create false readiness or leak private data."""
from __future__ import annotations

import contextlib
import io
import json
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

from local_agent.cli.diagnostics import build_parser
from local_agent.cli.overview import command_overview
from local_agent.cli.overview_human import render_overview_human

NOW = datetime(2026, 10, 21, 18, 0, tzinfo=timezone.utc)


def _report() -> dict[str, object]:
    return {
        "status": "healthy",
        "health": {"status": "healthy", "status_age_seconds": 12},
        "summary": {
            "active_workers_observed": True,
            "active_worker_count": 2,
            "max_parallel_workers_observed": 4,
            "record_counts": {"claims": 1, "result-spool": 0, "corrupt-claims": 0},
        },
        "warnings": [],
        "consistent_point_in_time": False,
        "execution_authority": "not_asserted",
        "mutated": False,
    }


class HumanOverviewTests(unittest.TestCase):
    def test_readable_status_workers_and_unknown_resources(self) -> None:
        text = render_overview_human(_report(), now=NOW)
        self.assertIn("Local Agent: healthy", text)
        self.assertIn("heartbeat 12s old", text)
        self.assertIn("Observed workers: 2/4", text)
        self.assertIn("NOT free slots", text)
        self.assertIn("claims=1", text)
        self.assertIn("not sampled (--with-host-resources)", text)
        self.assertIn("no admission, restart or replay authority", text)

    def test_missing_or_stale_samples_never_claim_free_capacity(self) -> None:
        report = _report()
        report["status"] = "unverified"
        report["health"] = {"status": "unverified", "status_age_seconds": 590}
        report["summary"] = {
            "active_workers_observed": False,
            "active_worker_count": None,
            "max_parallel_workers_observed": None,
            "record_counts": {"claims": None, "result-spool": None},
        }
        report["host_resources"] = {
            "capacity": {"memory_available_bytes": 8 * 1024**3,
                         "root_free_bytes": 100 * 1024**3,
                         "memory_availability_kind": "linux_memavailable",
                         "load_average_1m": 1.5, "load_average_5m": 2.25,
                         "load_average_15m": 3.125},
            "profile": {"logical_cpu_count": 8},
            "tools": [],
            "observed_at_utc": (NOW - timedelta(seconds=240)).isoformat(),
        }
        text = render_overview_human(report, now=NOW)
        self.assertIn("Local Agent: unverified", text)
        self.assertIn("heartbeat 590s old", text)
        self.assertIn("workers: unverified (NOT zero)", text)
        self.assertIn("claims=unverified", text)
        self.assertIn("stale (240s old)", text)
        self.assertIn("Linux MemAvailable estimate, not reserved", text)
        self.assertIn("OS load 1/5/15m: 1.50 / 2.25 / 3.12", text)
        self.assertIn("NOT CPU utilization %", text)
        self.assertNotIn("free slots: 4", text)

    def test_invalid_capacity_time_and_untrusted_version_are_sanitized(self) -> None:
        report = _report()
        report["host_resources"] = {
            "capacity": {"memory_available_bytes": None,
                         "root_free_bytes": None},
            "profile": {},
            "observed_at_utc": "not-a-date",
            "tools": [{
                "name": "git", "present": True, "error": None,
                "version": "git 3.0\x1b[31m\n/private/owner/secret",
            }],
        }
        text = render_overview_human(report, now=NOW)
        self.assertIn("sample: unverified", text)
        self.assertIn("RAM available: unverified", text)
        self.assertIn("Tool git: git 3.0", text)
        self.assertNotIn("[31m", text)
        self.assertNotIn("\x1b", text)
        self.assertNotIn("/private/owner/secret", text)
        report["host_resources"]["observed_at_utc"] = (NOW + timedelta(minutes=5)).isoformat()
        self.assertIn("future timestamp", render_overview_human(report, now=NOW))

    def test_cli_flag_opt_in_preserves_json_default_and_exit_status(self) -> None:
        basic = build_parser().parse_args(["overview"])
        human = build_parser().parse_args(["overview", "--human"])
        with mock.patch("local_agent.cli.overview.observe_local_overview",
                        return_value=_report()), mock.patch(
                            "local_agent.cli.overview.HostOverviewCollector",
                            side_effect=AssertionError("not requested")
                        ):
            with contextlib.redirect_stdout(io.StringIO()) as output:
                self.assertEqual(command_overview(basic), 0)
            self.assertEqual(json.loads(output.getvalue())["status"], "healthy")
            self.assertNotIn("Physical resources:", output.getvalue())

            with contextlib.redirect_stdout(io.StringIO()) as output:
                self.assertEqual(command_overview(human), 0)
            self.assertIn("Local Agent: healthy", output.getvalue())
            self.assertIn("Physical resources: not sampled", output.getvalue())

        missing = _report()
        missing["status"] = "unverified"
        with mock.patch("local_agent.cli.overview.observe_local_overview",
                        return_value=missing), contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(command_overview(human), 2)
        self.assertIn("Local Agent: unverified", output.getvalue())


if __name__ == "__main__":
    unittest.main()
