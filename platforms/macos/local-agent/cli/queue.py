"""Thin CLI adapter for read-only local Git project task/result observations."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import local_agent.daemon.service as agentd
from local_agent.runtime.queue_snapshot import observe_local_task_queue


def command_queue(args: argparse.Namespace) -> int:
    """Observe one local project control snapshot; never sync or admit tasks."""
    report = observe_local_task_queue(
        registry_file=args.registry_file or agentd.multirepo_registry_path(),
        repository_id=args.repository_id,
    )
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0 if report["status"] == "observed" else 2


def register_queue_parser(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser(
        "queue", help="inspect one local Git control commit (no fetch or task replay)"
    )
    parser.add_argument("--repository-id", required=True, help="enabled local repository ID")
    parser.add_argument("--registry-file", type=Path, help="local project registry")
    parser.set_defaults(func=command_queue)
