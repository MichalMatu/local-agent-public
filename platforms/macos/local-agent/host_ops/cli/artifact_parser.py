"""Argument parser for bounded local artifact operations."""

from __future__ import annotations

import argparse

from local_agent.host_ops.capabilities.local.files import DEFAULT_MAX_ARTIFACT_BYTES


def add_artifact_arguments(parser: argparse.ArgumentParser, add_json_argument) -> None:
    subparsers = parser.add_subparsers(dest="artifact_command", required=True)

    inspect_parser = subparsers.add_parser(
        "inspect",
        help="inspect one regular file and report digest evidence",
    )
    inspect_parser.add_argument("source", help="regular file to inspect")
    inspect_parser.add_argument(
        "--max-bytes",
        type=int,
        default=DEFAULT_MAX_ARTIFACT_BYTES,
        help="maximum artifact size in bytes (default: 512 MiB; hard max: 16 GiB)",
    )
    inspect_parser.add_argument(
        "--timeout",
        type=float,
        default=300.0,
        dest="timeout_seconds",
        help="whole artifact inspection deadline in seconds (default: 300)",
    )
    add_json_argument(inspect_parser)

    deploy_parser = subparsers.add_parser(
        "deploy",
        help="copy one artifact into an existing local directory",
    )
    deploy_parser.add_argument("source", help="source regular file")
    deploy_parser.add_argument("destination_directory", help="existing destination directory")
    deploy_parser.add_argument(
        "--name",
        default=None,
        dest="destination_name",
        help="destination filename; source basename is used by default",
    )
    deploy_parser.add_argument(
        "--replace",
        action="store_true",
        help="explicitly allow replacing an existing regular destination file",
    )
    deploy_parser.add_argument(
        "--max-bytes",
        type=int,
        default=DEFAULT_MAX_ARTIFACT_BYTES,
        help="maximum artifact size in bytes (default: 512 MiB; hard max: 16 GiB)",
    )
    deploy_parser.add_argument(
        "--timeout",
        type=float,
        default=300.0,
        dest="timeout_seconds",
        help="whole artifact deployment deadline in seconds (default: 300)",
    )
    add_json_argument(deploy_parser)
