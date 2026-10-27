"""Thin read-only CLI adapter for repository lease-file inventory."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import local_agent.daemon.service as agentd
from local_agent.runtime.lease_snapshot import observe_local_lease_inventory


def command_leases(args: argparse.Namespace) -> int:
    """Present Core metadata; never acquire a lock or infer a holder."""
    payload = observe_local_lease_inventory(
        state_root=args.state_root or agentd.STATE_DIR,
        registry_file=args.registry_file or agentd.multirepo_registry_path(),
    )
    print(json.dumps(payload, indent=2, ensure_ascii=False))
    return 0 if payload["status"] == "observed" else 2


def register_leases_parser(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser(
        "leases", help="count stored repository lease files (not active lock holders)"
    )
    parser.add_argument("--state-root", type=Path, help="local state directory")
    parser.add_argument("--registry-file", type=Path, help="local repository registry")
    parser.set_defaults(func=command_leases)
