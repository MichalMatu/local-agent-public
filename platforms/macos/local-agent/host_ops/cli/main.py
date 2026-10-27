"""hostops command-line entrypoint."""

from __future__ import annotations

import argparse
from collections.abc import Sequence

from local_agent.host_ops.version import JSON_CONTRACT_VERSION, package_version

from .commands import (
    adb,
    artifact,
    artifact_inspect,
    browser,
    doctor,
    host,
    macos,
    network,
    remote_git,
    removable_media,
    serial,
    ssh,
    tools,
)
from .parser_support import (
    add_adb_arguments as _add_adb_arguments,
)
from .artifact_parser import add_artifact_arguments as _add_artifact_arguments
from .parser_support import (
    add_browser_arguments as _add_browser_arguments,
)
from .parser_support import (
    add_host_arguments as _add_host_arguments,
)
from .parser_support import (
    add_json_argument as _add_json_argument,
)
from .parser_support import (
    add_macos_arguments as _add_macos_arguments,
)
from .parser_support import (
    add_remote_git_cache_arguments as _add_remote_git_cache_arguments,
)
from .parser_support import (
    add_ssh_arguments as _add_ssh_arguments,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="hostops",
        description="Deterministic host capability layer for Local Agent execution.",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {package_version()}",
    )
    parser.add_argument(
        "--json-contract-version",
        action="version",
        version=str(JSON_CONTRACT_VERSION),
        help="print the machine-readable JSON output contract version and exit",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    doctor_parser = subparsers.add_parser("doctor", help="inspect local prerequisites")
    _add_json_argument(doctor_parser)

    tools_parser = subparsers.add_parser(
        "tools",
        help="inspect explicitly named local executables",
    )
    _add_tools_arguments(tools_parser)

    adb_parser = subparsers.add_parser(
        "adb",
        help="inspect local Android Debug Bridge devices",
    )
    _add_adb_arguments(adb_parser)

    host_parser = subparsers.add_parser(
        "host",
        help="inspect generic local host capability facts",
    )
    _add_host_arguments(host_parser)

    browser_parser = subparsers.add_parser(
        "browser",
        help="inspect local browser processes and explicit Chromium DevTools endpoints",
    )
    _add_browser_arguments(browser_parser)

    artifact_parser = subparsers.add_parser(
        "artifact",
        help="inspect or deploy local artifacts",
    )
    _add_artifact_arguments(artifact_parser, _add_json_argument)

    network_parser = subparsers.add_parser(
        "network",
        help="perform bounded local DNS and TCP probes",
    )
    _add_network_arguments(network_parser)

    serial_parser = subparsers.add_parser(
        "serial",
        help="perform bounded local POSIX serial transactions",
    )
    _add_serial_arguments(serial_parser)

    macos_parser = subparsers.add_parser(
        "macos",
        help="inspect or control local macOS devices",
    )
    _add_macos_arguments(macos_parser)

    ssh_parser = subparsers.add_parser("ssh", help="operate configured SSH targets")
    _add_ssh_arguments(ssh_parser)

    remote_parser = subparsers.add_parser(
        "remote",
        help="run reusable workflows on configured remote hosts",
    )
    remote_subparsers = remote_parser.add_subparsers(dest="remote_command", required=True)
    remote_git_parser = remote_subparsers.add_parser(
        "git",
        help="prepare and run exact-revision Git workspaces",
    )
    remote_git_subparsers = remote_git_parser.add_subparsers(
        dest="remote_git_command",
        required=True,
    )

    prepare_parser = remote_git_subparsers.add_parser(
        "prepare",
        help="prepare an explicitly supplied exact Git revision",
    )
    _add_remote_git_explicit_arguments(prepare_parser)

    run_parser = remote_git_subparsers.add_parser(
        "run",
        help="prepare an explicitly supplied exact Git revision and run a command",
    )
    _add_remote_git_explicit_arguments(run_parser)
    _add_remote_command_argument(run_parser)

    prepare_current_parser = remote_git_subparsers.add_parser(
        "prepare-current",
        help="derive repository URL and exact HEAD from a local Git checkout",
    )
    _add_remote_git_current_arguments(prepare_current_parser)

    run_current_parser = remote_git_subparsers.add_parser(
        "run-current",
        help="run a remote command against the current local repository HEAD",
    )
    _add_remote_git_current_arguments(run_current_parser)
    _add_remote_command_argument(run_current_parser)

    cache_parser = remote_git_subparsers.add_parser(
        "cache",
        help="inspect or remove deterministic remote Git cache workspaces",
    )
    _add_remote_git_cache_arguments(cache_parser)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "doctor":
        return doctor.run(as_json=args.as_json)
    if args.command == "tools" and args.tools_command == "inspect":
        return tools.run_inspect(
            args.names,
            timeout_seconds=args.timeout_seconds,
            as_json=args.as_json,
        )
    if args.command == "adb":
        return _run_adb(args)
    if args.command == "host" and args.host_command == "profile":
        return host.run_profile(
            timeout_seconds=args.timeout_seconds,
            as_json=args.as_json,
        )
    if args.command == "host" and args.host_command == "capacity":
        return host.run_capacity(
            timeout_seconds=args.timeout_seconds,
            as_json=args.as_json,
        )
    if args.command == "host" and args.host_command == "overview":
        return host.run_overview(
            timeout_seconds=args.timeout_seconds,
            names=tuple(args.tools),
            as_json=args.as_json,
        )
    if args.command == "browser":
        return browser.run_command(args)
    if args.command == "artifact" and args.artifact_command == "inspect":
        return artifact_inspect.run(
            args.source,
            max_bytes=args.max_bytes,
            timeout_seconds=args.timeout_seconds,
            as_json=args.as_json,
        )
    if args.command == "artifact" and args.artifact_command == "deploy":
        return artifact.run_deploy(
            args.source,
            args.destination_directory,
            destination_name=args.destination_name,
            replace=args.replace,
            max_bytes=args.max_bytes,
            timeout_seconds=args.timeout_seconds,
            as_json=args.as_json,
        )
    if args.command == "network" and args.network_command == "resolve":
        return network.run_resolve(
            args.host,
            timeout_seconds=args.timeout_seconds,
            as_json=args.as_json,
        )
    if args.command == "network" and args.network_command == "tcp":
        return network.run_tcp(
            args.host,
            args.port,
            timeout_seconds=args.timeout_seconds,
            as_json=args.as_json,
        )
    if args.command == "serial" and args.serial_command == "transact":
        return serial.run_transact(
            args.port,
            baudrate=args.baudrate,
            write_text=args.write_text,
            write_hex=args.write_hex,
            read_limit=args.read_limit,
            timeout_seconds=args.timeout_seconds,
            idle_seconds=args.idle_seconds,
            settle_seconds=args.settle_seconds,
            as_json=args.as_json,
        )
    if args.command == "macos":
        return _run_macos(args)
    if args.command == "ssh" and args.ssh_command == "check":
        return ssh.run_check(
            args.target,
            timeout_seconds=args.timeout_seconds,
            as_json=args.as_json,
        )
    if args.command == "ssh" and args.ssh_command == "exec":
        return ssh.run_exec(
            args.target,
            args.remote_argv,
            timeout_seconds=args.timeout_seconds,
            as_json=args.as_json,
        )
    if args.command == "ssh" and args.ssh_command == "push":
        return ssh.run_push(
            args.target,
            args.local_source,
            args.remote_destination,
            replace=args.replace,
            max_bytes=args.max_bytes,
            timeout_seconds=args.timeout_seconds,
            as_json=args.as_json,
        )
    if args.command == "ssh" and args.ssh_command == "pull":
        return ssh.run_pull(
            args.target,
            args.remote_source,
            args.local_destination,
            replace=args.replace,
            max_bytes=args.max_bytes,
            timeout_seconds=args.timeout_seconds,
            as_json=args.as_json,
        )
    if args.command == "remote" and args.remote_command == "git":
        if args.remote_git_command == "cache":
            if args.remote_git_cache_command == "list":
                return remote_git.run_cache_list(
                    args.target,
                    timeout_seconds=args.timeout_seconds,
                    as_json=args.as_json,
                )
            if args.remote_git_cache_command == "remove":
                return remote_git.run_cache_remove(
                    args.target,
                    args.workspace,
                    timeout_seconds=args.timeout_seconds,
                    as_json=args.as_json,
                )
            raise AssertionError(
                f"unhandled remote Git cache command: {args.remote_git_cache_command}"
            )

        execution = {
            "lock": args.lock,
            "clean_mode": args.clean_mode,
            "timeout_seconds": args.timeout_seconds,
            "as_json": args.as_json,
        }
        if args.remote_git_command in {"prepare", "run"}:
            explicit = {
                "repository_url": args.repository_url,
                "revision": args.revision,
                "workspace_name": args.workspace_name,
                **execution,
            }
            if args.remote_git_command == "prepare":
                return remote_git.run_prepare(args.target, **explicit)
            return remote_git.run_command(args.target, args.remote_argv, **explicit)

        current = {
            "source_path": args.source_path,
            "remote_name": args.remote_name,
            "repository_url_override": args.repository_url_override,
            "workspace_name": args.workspace_name,
            **execution,
        }
        if args.remote_git_command == "prepare-current":
            return remote_git.run_prepare_current(args.target, **current)
        if args.remote_git_command == "run-current":
            return remote_git.run_current(args.target, args.remote_argv, **current)
    raise AssertionError(f"unhandled command: {args.command}")


def _run_macos(args: argparse.Namespace) -> int:
    if args.macos_command == "host":
        return macos.run_host(as_json=args.as_json)
    if args.macos_command == "usb":
        return macos.run_usb(as_json=args.as_json)
    if args.macos_command == "serial":
        return macos.run_serial(as_json=args.as_json)
    if args.macos_command == "storage":
        return macos.run_storage(as_json=args.as_json)
    if args.macos_command == "deploy-media":
        return removable_media.run_deploy(
            args.identifier,
            args.source,
            destination_name=args.destination_name,
            replace=args.replace,
            eject=args.eject,
            timeout_seconds=args.timeout_seconds,
            as_json=args.as_json,
        )
    if args.macos_command in {"mount", "unmount", "eject"}:
        return macos.run_storage_action(
            args.macos_command,
            args.identifier,
            as_json=args.as_json,
        )
    raise AssertionError(f"unhandled macOS command: {args.macos_command}")


def _run_adb(args: argparse.Namespace) -> int:
    if args.adb_command == "devices":
        return adb.run_devices(
            timeout_seconds=args.timeout_seconds,
            as_json=args.as_json,
        )
    if args.adb_command == "identity":
        return adb.run_identity(
            args.serial,
            timeout_seconds=args.timeout_seconds,
            as_json=args.as_json,
        )
    if args.adb_command == "logcat":
        return adb.run_logcat(
            args.serial,
            lines=args.lines,
            timeout_seconds=args.timeout_seconds,
            as_json=args.as_json,
        )
    if args.adb_command == "push":
        return adb.run_push(
            args.serial,
            args.local_source,
            args.remote_destination,
            replace=args.replace,
            max_bytes=args.max_bytes,
            timeout_seconds=args.timeout_seconds,
            as_json=args.as_json,
        )
    if args.adb_command == "pull":
        return adb.run_pull(
            args.serial,
            args.remote_source,
            args.local_destination,
            replace=args.replace,
            max_bytes=args.max_bytes,
            timeout_seconds=args.timeout_seconds,
            as_json=args.as_json,
        )
    raise AssertionError(f"unhandled ADB command: {args.adb_command}")


def _add_tools_arguments(parser: argparse.ArgumentParser) -> None:
    tool_subparsers = parser.add_subparsers(dest="tools_command", required=True)
    inspect_parser = tool_subparsers.add_parser(
        "inspect",
        help="report PATH presence and bounded --version evidence",
    )
    inspect_parser.add_argument(
        "names",
        nargs="+",
        metavar="TOOL",
        help="executable basenames to inspect",
    )
    inspect_parser.add_argument(
        "--timeout",
        type=float,
        default=5.0,
        dest="timeout_seconds",
        help="per-tool version probe timeout in seconds (default: 5)",
    )
    _add_json_argument(inspect_parser)


def _add_network_arguments(parser: argparse.ArgumentParser) -> None:
    network_subparsers = parser.add_subparsers(dest="network_command", required=True)
    resolve_parser = network_subparsers.add_parser(
        "resolve",
        help="resolve one hostname or IP address with a hard deadline",
    )
    resolve_parser.add_argument("host", help="hostname or IP address")
    _add_network_timeout(resolve_parser)

    tcp_parser = network_subparsers.add_parser(
        "tcp",
        help="attempt one bounded TCP connection",
    )
    tcp_parser.add_argument("host", help="hostname or IP address")
    tcp_parser.add_argument("port", type=int, help="TCP port from 1 to 65535")
    _add_network_timeout(tcp_parser)


def _add_network_timeout(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--timeout",
        type=float,
        default=5.0,
        dest="timeout_seconds",
        help="whole DNS/connect operation deadline in seconds (default: 5)",
    )
    _add_json_argument(parser)


def _add_serial_arguments(parser: argparse.ArgumentParser) -> None:
    serial_subparsers = parser.add_subparsers(dest="serial_command", required=True)
    transact_parser = serial_subparsers.add_parser(
        "transact",
        help="write optional bytes and read a bounded response from one serial port",
    )
    transact_parser.add_argument("port", help="absolute serial device path below /dev")
    transact_parser.add_argument("--baud", type=int, required=True, dest="baudrate")
    write_group = transact_parser.add_mutually_exclusive_group()
    write_group.add_argument(
        "--write-text",
        default=None,
        help="UTF-8 text to write before reading",
    )
    write_group.add_argument(
        "--write-hex",
        default=None,
        help="hexadecimal bytes to write before reading",
    )
    transact_parser.add_argument(
        "--read-limit",
        type=int,
        default=4096,
        help="maximum response bytes to retain (default: 4096)",
    )
    transact_parser.add_argument(
        "--timeout",
        type=float,
        default=1.0,
        dest="timeout_seconds",
        help="whole transaction deadline in seconds (default: 1.0)",
    )
    transact_parser.add_argument(
        "--idle",
        type=float,
        default=0.1,
        dest="idle_seconds",
        help="finish after this much response silence (default: 0.1)",
    )
    transact_parser.add_argument(
        "--settle",
        type=float,
        default=0.0,
        dest="settle_seconds",
        help="delay I/O after opening the port, within the transaction deadline (default: 0)",
    )
    _add_json_argument(transact_parser)


def _add_remote_git_explicit_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("target", help="host alias from the local host-ops configuration")
    parser.add_argument(
        "--repo",
        required=True,
        dest="repository_url",
        help="remote-visible Git URL",
    )
    parser.add_argument(
        "--revision",
        required=True,
        help="full lowercase 40- or 64-hex Git object id",
    )
    parser.add_argument(
        "--workspace",
        required=True,
        dest="workspace_name",
        help="stable cache/workspace key",
    )
    _add_remote_git_execution_arguments(parser)


def _add_remote_git_current_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("target", help="host alias from the local host-ops configuration")
    parser.add_argument(
        "--repo-path",
        default=".",
        dest="source_path",
        help="local path inside the source Git repository (default: current directory)",
    )
    parser.add_argument(
        "--remote",
        default="origin",
        dest="remote_name",
        help="local Git remote used by the worker (default: origin)",
    )
    parser.add_argument(
        "--repo-url",
        default=None,
        dest="repository_url_override",
        help="optional worker-visible Git URL override; local remote URL is used by default",
    )
    parser.add_argument(
        "--workspace",
        default=None,
        dest="workspace_name",
        help="optional remote workspace key; derived from repository identity by default",
    )
    _add_remote_git_execution_arguments(parser)


def _add_remote_git_execution_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--lock",
        default=None,
        help="optional host-wide lock key for serializing heavy jobs",
    )
    parser.add_argument(
        "--clean",
        choices=("worktree", "full"),
        default="worktree",
        dest="clean_mode",
        help="worktree preserves ignored caches; full removes ignored files too",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=3600.0,
        dest="timeout_seconds",
        help="whole remote workflow timeout in seconds (default: 3600)",
    )
    _add_json_argument(parser)


def _add_remote_command_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "remote_argv",
        nargs="+",
        metavar="REMOTE_ARG",
        help="command argv executed from the prepared repository; use -- before flags",
    )
