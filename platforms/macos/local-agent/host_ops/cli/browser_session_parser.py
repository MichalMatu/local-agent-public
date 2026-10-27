"""Argparse builder for persistent managed browser sessions."""

from __future__ import annotations

import argparse
from collections.abc import Callable

JsonArgumentAdder = Callable[[argparse.ArgumentParser], None]


def _add_profile_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--profile-dir",
        required=True,
        dest="profile_dir",
        help="absolute whitespace-free isolated profile directory owned by this session",
    )


def _add_browser_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--browser-executable",
        required=True,
        dest="browser_executable",
        help="absolute Chromium-family browser executable path",
    )


def _add_start_url_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--url",
        default=None,
        dest="start_url",
        help="optional sanitized HTTP(S) start URL with no credentials, query or fragment",
    )


def add_browser_session_arguments(
    parser: argparse.ArgumentParser,
    add_json_argument: JsonArgumentAdder,
) -> None:
    session_subparsers = parser.add_subparsers(dest="browser_session_command", required=True)

    start_parser = session_subparsers.add_parser(
        "start",
        help="start one Chromium process with an isolated host-ops-owned profile and loopback CDP",
    )
    _add_profile_argument(start_parser)
    _add_browser_argument(start_parser)
    start_parser.add_argument(
        "--extension-dir",
        action="append",
        default=[],
        dest="extension_dirs",
        metavar="PATH",
        help="absolute unpacked extension directory; repeat up to 8 times",
    )
    _add_start_url_argument(start_parser)
    start_parser.add_argument(
        "--download-dir",
        default=None,
        dest="download_dir",
        metavar="PATH",
        help="optional absolute non-symlink directory for browser downloads",
    )
    start_parser.add_argument(
        "--timeout",
        type=float,
        default=15.0,
        dest="timeout_seconds",
        help="whole browser startup deadline in seconds, at least 1 and at most 120 (default: 15)",
    )
    add_json_argument(start_parser)

    status_parser = session_subparsers.add_parser(
        "status",
        help="inspect one exact host-ops-owned managed browser profile",
    )
    _add_profile_argument(status_parser)
    status_parser.add_argument(
        "--timeout",
        type=float,
        default=5.0,
        dest="timeout_seconds",
        help="bounded process/endpoint inspection timeout in seconds (default: 5)",
    )
    add_json_argument(status_parser)

    stop_parser = session_subparsers.add_parser(
        "stop",
        help="send SIGTERM only to the exact browser process using the managed profile",
    )
    _add_profile_argument(stop_parser)
    stop_parser.add_argument(
        "--timeout",
        type=float,
        default=10.0,
        dest="timeout_seconds",
        help="bounded graceful shutdown deadline in seconds (default: 10)",
    )
    add_json_argument(stop_parser)

    interactive_start = session_subparsers.add_parser(
        "interactive-start",
        help="start an exact owned profile without CDP or extension flags for user interaction",
    )
    _add_profile_argument(interactive_start)
    _add_browser_argument(interactive_start)
    _add_start_url_argument(interactive_start)
    interactive_start.add_argument(
        "--timeout",
        type=float,
        default=15.0,
        dest="timeout_seconds",
        help="whole interactive startup deadline in seconds (default: 15)",
    )
    add_json_argument(interactive_start)

    interactive_status = session_subparsers.add_parser(
        "interactive-status",
        help="inspect an exact owned profile running without CDP",
    )
    _add_profile_argument(interactive_status)
    interactive_status.add_argument(
        "--timeout",
        type=float,
        default=5.0,
        dest="timeout_seconds",
        help="bounded process inspection timeout in seconds (default: 5)",
    )
    add_json_argument(interactive_status)

    interactive_stop = session_subparsers.add_parser(
        "interactive-stop",
        help="stop only the exact no-CDP browser using the owned profile",
    )
    _add_profile_argument(interactive_stop)
    interactive_stop.add_argument(
        "--clear-session-restore",
        action="store_true",
        help="after the exact process exits, remove only tab/session restore state",
    )
    interactive_stop.add_argument(
        "--timeout",
        type=float,
        default=10.0,
        dest="timeout_seconds",
        help="bounded graceful shutdown deadline in seconds (default: 10)",
    )
    add_json_argument(interactive_stop)
