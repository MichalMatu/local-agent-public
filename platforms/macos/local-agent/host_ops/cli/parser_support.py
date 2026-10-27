"""Cohesive argparse builders shared by the hostops CLI entrypoint."""

from __future__ import annotations

import argparse
from typing import Any

from local_agent.host_ops.capabilities.local.adb import (
    DEFAULT_MAX_TRANSFER_BYTES as DEFAULT_ADB_MAX_TRANSFER_BYTES,
)
from local_agent.host_ops.capabilities.remote.ssh import (
    DEFAULT_MAX_TRANSFER_BYTES as DEFAULT_SSH_MAX_TRANSFER_BYTES,
)
from local_agent.host_ops.cli.browser_session_parser import add_browser_session_arguments


def add_json_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--json",
        action="store_true",
        dest="as_json",
        help="emit machine-readable JSON",
    )


def add_adb_arguments(parser: argparse.ArgumentParser) -> None:
    subparsers = parser.add_subparsers(dest="adb_command", required=True)
    devices_parser = subparsers.add_parser(
        "devices",
        help="list attached ADB device identities and authorization state",
    )
    _add_adb_common_arguments(devices_parser)

    identity_parser = subparsers.add_parser(
        "identity",
        help="inspect one ready ADB device by explicit serial",
    )
    identity_parser.add_argument("serial", help="exact ADB device serial from adb devices")
    _add_adb_common_arguments(identity_parser)

    logcat_parser = subparsers.add_parser(
        "logcat",
        help="capture a bounded recent logcat tail from one explicit ready device",
    )
    logcat_parser.add_argument("serial", help="exact ADB device serial from adb devices")
    logcat_parser.add_argument(
        "--lines",
        type=int,
        default=200,
        help="maximum recent log lines requested from adb logcat (default: 200)",
    )
    _add_adb_common_arguments(logcat_parser)

    push_parser = subparsers.add_parser(
        "push",
        help="upload one verified regular file to one explicit ready ADB device",
    )
    push_parser.add_argument("serial", help="exact ADB device serial from adb devices")
    push_parser.add_argument("local_source", help="local regular file to upload")
    push_parser.add_argument("remote_destination", help="absolute remote destination file path")
    _add_adb_transfer_arguments(push_parser)

    pull_parser = subparsers.add_parser(
        "pull",
        help="download one verified regular file from one explicit ready ADB device",
    )
    pull_parser.add_argument("serial", help="exact ADB device serial from adb devices")
    pull_parser.add_argument("remote_source", help="absolute remote source file path")
    pull_parser.add_argument("local_destination", help="local destination file path")
    _add_adb_transfer_arguments(pull_parser)



def add_browser_arguments(parser: argparse.ArgumentParser) -> None:
    subparsers = parser.add_subparsers(dest="browser_command", required=True)
    inspect_parser = subparsers.add_parser(
        "inspect",
        help="list browser processes and probe explicit/discovered local DevTools endpoints",
    )
    inspect_parser.add_argument(
        "--endpoint",
        action="append",
        default=[],
        dest="endpoints",
        metavar="URL",
        help="explicit loopback DevTools base URL, for example http://127.0.0.1:9222",
    )
    inspect_parser.add_argument(
        "--timeout",
        type=float,
        default=5.0,
        dest="timeout_seconds",
        help="bounded process and per-request probe timeout in seconds (default: 5)",
    )
    add_json_argument(inspect_parser)

    probe_parser = subparsers.add_parser(
        "probe",
        help="navigate once in an isolated managed browser context and report bounded metadata",
    )
    probe_parser.add_argument("url", help="explicit HTTP(S) URL to navigate")
    probe_parser.add_argument(
        "--engine",
        choices=("chromium", "firefox", "webkit"),
        default="chromium",
        help="managed Playwright browser engine (default: chromium)",
    )
    probe_parser.add_argument(
        "--timeout",
        type=float,
        default=15.0,
        dest="timeout_seconds",
        help="whole managed-probe deadline in seconds, at least 1 and at most 120 (default: 15)",
    )
    add_json_argument(probe_parser)

    session_parser = subparsers.add_parser(
        "session",
        help="start, inspect or stop one isolated persistent Chromium CDP session",
    )
    add_browser_session_arguments(session_parser, add_json_argument)

    attach_parser = subparsers.add_parser(
        "attach",
        help="attach to one explicit local Chromium CDP endpoint",
    )
    _add_browser_attach_arguments(attach_parser)


def _add_browser_attach_arguments(parser: argparse.ArgumentParser) -> None:
    attach_subparsers = parser.add_subparsers(dest="browser_attach_command", required=True)
    attach_inspect_parser = attach_subparsers.add_parser(
        "inspect",
        help="list bounded Chromium CDP target metadata without page mutation",
    )
    attach_inspect_parser.add_argument(
        "--endpoint",
        required=True,
        help="explicit loopback Chromium DevTools base URL",
    )
    attach_inspect_parser.add_argument(
        "--timeout",
        type=float,
        default=10.0,
        dest="timeout_seconds",
        help="whole CDP attach deadline in seconds, at least 1 and at most 120 (default: 10)",
    )
    add_json_argument(attach_inspect_parser)

    attach_snapshot_parser = attach_subparsers.add_parser(
        "snapshot",
        help="read bounded page/frame/navigation/document metadata for one exact page target",
    )
    attach_snapshot_parser.add_argument(
        "--endpoint", required=True, help="explicit loopback Chromium DevTools base URL"
    )
    attach_snapshot_parser.add_argument(
        "--target-id",
        required=True,
        dest="target_id",
        help="exact page target id from browser attach inspect",
    )
    attach_snapshot_parser.add_argument(
        "--timeout",
        type=float,
        default=10.0,
        dest="timeout_seconds",
        help="whole CDP snapshot deadline in seconds, at least 1 and at most 120 (default: 10)",
    )
    add_json_argument(attach_snapshot_parser)

    attach_selectors_parser = attach_subparsers.add_parser(
        "selectors",
        help="count bounded CSS selector matches for one exact page target",
    )
    attach_selectors_parser.add_argument(
        "--endpoint", required=True, help="explicit loopback Chromium DevTools base URL"
    )
    attach_selectors_parser.add_argument(
        "--target-id",
        required=True,
        dest="target_id",
        help="exact page target id from browser attach inspect",
    )
    attach_selectors_parser.add_argument(
        "--selector",
        action="append",
        required=True,
        dest="selectors",
        metavar="CSS",
        help="CSS selector to count; repeat for multiple selectors (max 16)",
    )
    attach_selectors_parser.add_argument(
        "--timeout",
        type=float,
        default=10.0,
        dest="timeout_seconds",
        help="whole selector-count deadline in seconds, at least 1 and at most 120 (default: 10)",
    )
    add_json_argument(attach_selectors_parser)
    _add_browser_readiness_arguments(attach_subparsers)
    _add_browser_content_script_recovery_arguments(attach_subparsers)

    attach_workers_parser = attach_subparsers.add_parser(
        "workers",
        help="read bounded worker and service-worker lifecycle diagnostics without mutation",
    )
    attach_workers_parser.add_argument(
        "--endpoint", required=True, help="explicit loopback Chromium DevTools base URL"
    )
    attach_workers_parser.add_argument(
        "--timeout",
        type=float,
        default=10.0,
        dest="timeout_seconds",
        help=(
            "whole worker-diagnostics deadline in seconds, at least 1 and at most 120 (default: 10)"
        ),
    )
    add_json_argument(attach_workers_parser)

    attach_reload_parser = attach_subparsers.add_parser(
        "reload",
        help="reload one exact HTTP(S) page target after an expected-URL guard",
    )
    attach_reload_parser.add_argument(
        "--endpoint", required=True, help="explicit loopback Chromium DevTools base URL"
    )
    attach_reload_parser.add_argument(
        "--target-id",
        required=True,
        dest="target_id",
        help="exact page target id from browser attach inspect",
    )
    attach_reload_parser.add_argument(
        "--expect-url",
        required=True,
        dest="expected_url",
        help="sanitized HTTP(S) target URL previously returned by attach inspect/snapshot",
    )
    attach_reload_parser.add_argument(
        "--timeout",
        type=float,
        default=15.0,
        dest="timeout_seconds",
        help="whole guarded-reload deadline in seconds, at least 1 and at most 120 (default: 15)",
    )
    add_json_argument(attach_reload_parser)


def _add_browser_readiness_arguments(attach_subparsers: Any) -> None:
    readiness_parser = attach_subparsers.add_parser(
        "readiness",
        help="correlate DOM selectors with exact extension script fingerprints read-only",
    )
    readiness_parser.add_argument(
        "--endpoint", required=True, help="explicit loopback Chromium DevTools base URL"
    )
    readiness_parser.add_argument(
        "--target-id",
        required=True,
        dest="target_id",
        help="exact page target id from browser attach inspect",
    )
    readiness_parser.add_argument(
        "--selector",
        action="append",
        required=True,
        dest="selectors",
        metavar="CSS",
        help="required DOM selector; repeat for multiple selectors (max 16)",
    )
    readiness_parser.add_argument(
        "--script-fingerprint",
        action="append",
        required=True,
        dest="script_fingerprints",
        metavar="NAME=SHA256",
        help="expected chrome-extension script basename and SHA-256; repeat up to 16 times",
    )
    readiness_parser.add_argument(
        "--timeout",
        type=float,
        default=10.0,
        dest="timeout_seconds",
        help="whole readiness deadline in seconds, at least 1 and at most 120 (default: 10)",
    )
    add_json_argument(readiness_parser)


def _add_browser_content_script_recovery_arguments(attach_subparsers: Any) -> None:
    recovery_parser = attach_subparsers.add_parser(
        "recover-content-script",
        help="reload once only for missing/stale extension content scripts, then re-check",
    )
    recovery_parser.add_argument(
        "--endpoint", required=True, help="explicit loopback Chromium DevTools base URL"
    )
    recovery_parser.add_argument(
        "--target-id",
        required=True,
        dest="target_id",
        help="exact page target id from browser attach inspect",
    )
    recovery_parser.add_argument(
        "--expect-url",
        required=True,
        dest="expected_url",
        help="sanitized HTTP(S) target URL previously returned by browser evidence",
    )
    recovery_parser.add_argument(
        "--selector",
        action="append",
        required=True,
        dest="selectors",
        metavar="CSS",
        help="required DOM selector; repeat for multiple selectors (max 16)",
    )
    recovery_parser.add_argument(
        "--script-fingerprint",
        action="append",
        required=True,
        dest="script_fingerprints",
        metavar="NAME=SHA256",
        help="expected chrome-extension script basename and SHA-256; repeat up to 16 times",
    )
    recovery_parser.add_argument(
        "--timeout",
        type=float,
        default=30.0,
        dest="timeout_seconds",
        help="whole recovery deadline in seconds, at least 3 and at most 120 (default: 30)",
    )
    add_json_argument(recovery_parser)


def add_host_arguments(parser: argparse.ArgumentParser) -> None:
    subparsers = parser.add_subparsers(dest="host_command", required=True)
    profile_parser = subparsers.add_parser(
        "profile",
        help="report hostname, OS, CPU, memory and root filesystem capacity",
    )
    profile_parser.add_argument(
        "--timeout",
        type=float,
        default=5.0,
        dest="timeout_seconds",
        help="bounded native probe timeout in seconds (default: 5)",
    )
    add_json_argument(profile_parser)

    capacity_parser = subparsers.add_parser(
        "capacity",
        help="read current available memory, load averages and root free space",
    )
    capacity_parser.add_argument(
        "--timeout",
        type=float,
        default=5.0,
        dest="timeout_seconds",
        help="bounded native probe timeout in seconds (default: 5)",
    )
    add_json_argument(capacity_parser)

    overview_parser = subparsers.add_parser(
        "overview",
        help="combine profile, live capacity and bounded named tool status",
    )
    overview_parser.add_argument(
        "--tools",
        nargs="+",
        default=("git", "python3", "node", "cmake", "ninja"),
        metavar="NAME",
        help="up to 12 executable basenames (default: git python3 node cmake ninja)",
    )
    overview_parser.add_argument(
        "--timeout",
        type=float,
        default=15.0,
        dest="timeout_seconds",
        help="whole-overview timeout in seconds (default: 15)",
    )
    add_json_argument(overview_parser)


def add_macos_arguments(parser: argparse.ArgumentParser) -> None:
    subparsers = parser.add_subparsers(dest="macos_command", required=True)
    for command, help_text in (
        ("host", "inspect macOS version and host architecture"),
        ("usb", "list attached USB devices"),
        ("serial", "list serial callout and dial-in devices"),
        ("storage", "list external physical disks and their volumes"),
    ):
        command_parser = subparsers.add_parser(command, help=help_text)
        add_json_argument(command_parser)

    for command, help_text in (
        ("mount", "mount one explicitly identified external storage target"),
        ("unmount", "unmount one explicitly identified external storage target"),
        ("eject", "eject one explicitly identified whole external disk"),
    ):
        command_parser = subparsers.add_parser(command, help=help_text)
        command_parser.add_argument(
            "identifier",
            help="diskutil identifier such as disk4 or disk4s1",
        )
        add_json_argument(command_parser)

    deploy_media_parser = subparsers.add_parser(
        "deploy-media",
        help="deploy one verified artifact to an explicit external volume",
    )
    deploy_media_parser.add_argument(
        "identifier",
        help="explicit external volume identifier such as disk4s1",
    )
    deploy_media_parser.add_argument("source", help="source regular file")
    deploy_media_parser.add_argument(
        "--name",
        default=None,
        dest="destination_name",
        help="destination filename; source basename is used by default",
    )
    deploy_media_parser.add_argument(
        "--replace",
        action="store_true",
        help="explicitly allow replacing an existing regular destination file",
    )
    deploy_media_parser.add_argument(
        "--eject",
        action="store_true",
        help="eject the containing whole external disk after verified deployment",
    )
    deploy_media_parser.add_argument(
        "--timeout",
        type=float,
        default=30.0,
        dest="timeout_seconds",
        help="bounded disk inspection/control timeout in seconds (default: 30)",
    )
    add_json_argument(deploy_media_parser)


def add_ssh_arguments(parser: argparse.ArgumentParser) -> None:
    subparsers = parser.add_subparsers(dest="ssh_command", required=True)

    check_parser = subparsers.add_parser(
        "check",
        help="verify SSH transport and configured remote-user identity",
    )
    _add_ssh_common_arguments(check_parser)

    exec_parser = subparsers.add_parser(
        "exec",
        help="execute one bounded command on a configured SSH target",
    )
    _add_ssh_common_arguments(exec_parser)
    exec_parser.add_argument(
        "remote_argv",
        nargs="+",
        metavar="REMOTE_ARG",
        help="remote argv; use -- before arguments beginning with '-'",
    )

    push_parser = subparsers.add_parser(
        "push",
        help="upload one verified regular file to a configured SSH target",
    )
    push_parser.add_argument("target", help="host alias from the local host-ops configuration")
    push_parser.add_argument("local_source", help="local regular file to upload")
    push_parser.add_argument("remote_destination", help="absolute remote destination file path")
    _add_ssh_transfer_arguments(push_parser)

    pull_parser = subparsers.add_parser(
        "pull",
        help="download one verified regular file from a configured SSH target",
    )
    pull_parser.add_argument("target", help="host alias from the local host-ops configuration")
    pull_parser.add_argument("remote_source", help="absolute remote source file path")
    pull_parser.add_argument("local_destination", help="local destination file path")
    _add_ssh_transfer_arguments(pull_parser)


def add_remote_git_cache_arguments(parser: argparse.ArgumentParser) -> None:
    subparsers = parser.add_subparsers(dest="remote_git_cache_command", required=True)
    list_parser = subparsers.add_parser(
        "list",
        help="list deterministic remote Git cache workspaces",
    )
    list_parser.add_argument("target", help="host alias from the local host-ops configuration")
    _add_remote_git_cache_common_arguments(list_parser)

    remove_parser = subparsers.add_parser(
        "remove",
        help="remove one explicitly named remote Git cache workspace",
    )
    remove_parser.add_argument("target", help="host alias from the local host-ops configuration")
    remove_parser.add_argument("workspace", help="exact remote Git workspace name")
    _add_remote_git_cache_common_arguments(remove_parser)


def _add_adb_common_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--timeout",
        type=float,
        default=10.0,
        dest="timeout_seconds",
        help="whole ADB inspection deadline in seconds (default: 10)",
    )
    add_json_argument(parser)


def _add_adb_transfer_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--replace",
        action="store_true",
        help="explicitly allow replacing an existing regular destination file",
    )
    parser.add_argument(
        "--max-bytes",
        type=int,
        default=DEFAULT_ADB_MAX_TRANSFER_BYTES,
        help="maximum ADB transfer size in bytes (default: 512 MiB)",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=300.0,
        dest="timeout_seconds",
        help="whole ADB transfer deadline in seconds (default: 300)",
    )
    add_json_argument(parser)


def _add_ssh_common_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("target", help="host alias from the local host-ops configuration")
    parser.add_argument(
        "--timeout",
        type=float,
        default=30.0,
        dest="timeout_seconds",
        help="whole local SSH process timeout in seconds (default: 30)",
    )
    add_json_argument(parser)


def _add_ssh_transfer_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--replace",
        action="store_true",
        help="explicitly allow replacing an existing regular destination file",
    )
    parser.add_argument(
        "--max-bytes",
        type=int,
        default=DEFAULT_SSH_MAX_TRANSFER_BYTES,
        help="maximum transfer size in bytes (default: 512 MiB)",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=300.0,
        dest="timeout_seconds",
        help="whole transfer deadline in seconds (default: 300)",
    )
    add_json_argument(parser)


def _add_remote_git_cache_common_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--timeout",
        type=float,
        default=30.0,
        dest="timeout_seconds",
        help="whole remote cache operation timeout in seconds (default: 30)",
    )
    add_json_argument(parser)
