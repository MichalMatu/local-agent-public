"""Thin CLI adapter for the shared read-only local host overview."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import local_agent.daemon.service as agentd
from local_agent.host_ops.capabilities.local.host import HostOverviewCollector, HostProfileError
from local_agent.host_ops.core.execution import ExecutionLimits
from local_agent.runtime.overview_snapshot import observe_local_overview
from local_agent.cli.overview_human import render_overview_human


def command_overview(args: argparse.Namespace) -> int:
    """Format the importable Core observation; never perform host effects."""
    try:
        report = observe_local_overview(
            status_file=args.status_file or agentd.LOCAL_STATUS_PATH,
            registry_file=args.registry_file or agentd.multirepo_registry_path(),
            state_root=args.state_root or agentd.STATE_DIR,
            max_age_seconds=args.max_age_seconds,
        )
    except (ValueError, OSError):
        report = {
            "schema_version": 1, "status": "unverified",
            "warnings": ["invalid_overview_request"],
            "execution_authority": "not_asserted",
            "replay_authority": "not_asserted",
            "mutated": False,
        }
    if getattr(args, "with_host_resources", False):
        try:
            report["host_resources"] = HostOverviewCollector().inspect(
                names=tuple(args.resource_tools),
                limits=ExecutionLimits(
                    timeout_seconds=args.resource_timeout,
                    max_stdout_bytes=8192,
                    max_stderr_bytes=8192,
                ),
            )
        except (HostProfileError, ValueError):
            # A requested missing probe cannot turn into an invented idle host.
            report["host_resources"] = {
                "status": "unverified",
                "interpretation": "diagnostic_only_not_reserved_capacity",
            }
            report["warnings"] = sorted(set([*report.get("warnings", []),
                                              "host_resources_unavailable"]))
            report["status"] = "unverified"
        # This is explicitly two non-atomic observations, never admission proof.
        report["consistent_point_in_time"] = False
    if getattr(args, "human", False):
        print(render_overview_human(report))
    else:
        print(json.dumps(report, indent=2, ensure_ascii=False))
    return {"healthy": 0, "attention": 1, "degraded": 1, "unverified": 2}[str(report["status"])]


def register_overview_parser(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser(
        "overview", help="read-only combined local health and durable-record view"
    )
    parser.add_argument("--status-file", type=Path, help="local status observation file")
    parser.add_argument("--registry-file", type=Path, help="local repository registry")
    parser.add_argument("--state-root", type=Path, help="local installation state root")
    parser.add_argument("--max-age-seconds", type=int, default=180)
    parser.add_argument(
        "--human", action="store_true",
        help="readable advisory status with evidence freshness; JSON is the default",
    )
    parser.add_argument(
        "--with-host-resources", action="store_true",
        help="also sample bounded physical capacity and named tools",
    )
    parser.add_argument(
        "--resource-timeout", type=float, default=15.0,
        help="whole host resource observation deadline in seconds (default: 15)",
    )
    parser.add_argument(
        "--resource-tools", nargs="+",
        default=("git", "python3", "node", "cmake", "ninja"),
        metavar="NAME", help="1-12 validated tool basenames to include",
    )
    parser.set_defaults(func=command_overview)
