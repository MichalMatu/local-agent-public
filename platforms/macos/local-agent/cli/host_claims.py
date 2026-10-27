"""Thin CLI adapter for safe local host-task claim observations."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import local_agent.daemon.service as agentd
from local_agent.runtime.host_claim_snapshot import observe_host_claims


def command_host_claims(args: argparse.Namespace) -> int:
    """Inspect local host claim history without executing or replaying work."""
    payload = observe_host_claims(state_root=args.state_root or agentd.STATE_DIR)
    print(json.dumps(payload, indent=2, ensure_ascii=False))
    return 0 if payload["status"] == "observed" else 2


def register_host_claims_parser(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser(
        "host-claims", help="read-only host claims; does not prove active locks"
    )
    parser.add_argument("--state-root", type=Path, help="explicit local state root")
    parser.set_defaults(func=command_host_claims)
