"""Thin CLI adapter for local durable result publication evidence."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import local_agent.daemon.service as agentd
from local_agent.runtime.publication_snapshot import observe_local_publications


def command_publications(args: argparse.Namespace) -> int:
    """Print read-only Core results; never publish, replay, clean or admit tasks."""
    payload = observe_local_publications(
        state_root=args.state_root or agentd.STATE_DIR,
        registry_file=args.registry_file or agentd.multirepo_registry_path(),
    )
    print(json.dumps(payload, indent=2, ensure_ascii=False))
    return 0 if payload["status"] == "observed" else 2


def register_publications_parser(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser(
        "publications", help="count durable result-spool files (never replay/publish)"
    )
    parser.add_argument("--state-root", type=Path, help="local installation state directory")
    parser.add_argument("--registry-file", type=Path, help="local repository registry")
    parser.set_defaults(func=command_publications)
