"""Thin operator CLI for the existing supervisor's user-owned polling preference."""
from __future__ import annotations

import argparse
from contextlib import redirect_stdout
import io
import json
from pathlib import Path

import local_agent.daemon.service as agentd
from local_agent.supervisor.polling import (
    PollingSettings, read_polling_settings, save_polling_settings,
)


def command_polling(args: argparse.Namespace) -> int:
    state_dir: Path = args.state_dir or agentd.STATE_DIR
    try:
        if args.polling_command == "show":
            settings = read_polling_settings(state_dir)
        elif args.polling_command == "fixed":
            settings = PollingSettings(mode="fixed", seconds=args.seconds)
            save_polling_settings(state_dir, settings)
        elif args.polling_command == "adaptive":
            settings = PollingSettings()
            save_polling_settings(state_dir, settings)
        else:
            raise ValueError("unknown polling command")
    except (OSError, ValueError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, sort_keys=True))
        return 2
    print(json.dumps({
        "ok": True,
        **settings.as_dict(),
        "reload": "automatic_on_updated_supervisor",
        "requires_restart": False,
    }, sort_keys=True))
    return 0


def register_polling_parser(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser(
        "polling", help="show or set host, control and project Git polling"
    )
    sub = parser.add_subparsers(dest="polling_command", required=True)
    for name, help_text in (
        ("show", "show the current selection"),
        ("adaptive", "activity-based 2/5/15-second polling (recommended)"),
        ("fixed", "use a fixed 2..300-second interval"),
    ):
        choice = sub.add_parser(name, help=help_text)
        choice.add_argument(
            "--state-dir", type=Path,
            help="private local installation state (mainly for offline tests)",
        )
        if name == "fixed":
            choice.add_argument("--seconds", type=int, required=True)
        choice.set_defaults(func=command_polling)


def _menu_doctor() -> None:
    """Show bounded human feedback; standalone doctor retains its JSON API."""
    from local_agent.cli.diagnostics import command_doctor

    print("Doctor: checking service, repositories and storage...", flush=True)
    report_buffer = io.StringIO()
    with redirect_stdout(report_buffer):
        exit_code = command_doctor(argparse.Namespace())
    try:
        report = json.loads(report_buffer.getvalue())
    except (json.JSONDecodeError, TypeError):
        print(f"Doctor returned exit {exit_code}; run 'doctor' for the detailed report.")
        return
    checks = report.get("checks", [])
    warnings = report.get("warnings", [])
    if not isinstance(checks, list) or not isinstance(warnings, list):
        print("Doctor returned an unexpected report; run 'doctor' for details.")
        return
    checked = sum(isinstance(item, dict) and item.get("ok") is True for item in checks)
    errors = [str(item.get("name", "unknown")) for item in checks
              if isinstance(item, dict) and item.get("ok") is not True]
    status = "PASS" if exit_code == 0 and report.get("ok") is True else "FAIL"
    print(f"Doctor {status}: {checked}/{len(checks)} checks passed; "
          f"{len(warnings)} warning(s).")
    for name in errors[:5]:
        print(f"  FAIL: {_safe_menu_text(name)}")
    if len(errors) > 5:
        print(f"  ... and {len(errors) - 5} more failures")
    for warning in warnings[:3]:
        print(f"  Warning: {_safe_menu_text(str(warning), 160)}")
    if len(warnings) > 3:
        print(f"  ... and {len(warnings) - 3} more warnings")
    if errors:
        print("  No repairs attempted. Inspect the detailed Doctor report before changes.")
    print("Full JSON: python -m local_agent.cli.diagnostics doctor")



def _safe_menu_text(value: object, limit: int = 112) -> str:
    """Bound terminal output; never execute control codes from local status JSON."""
    if not isinstance(value, str) or not value:
        return "unverified"
    return "".join(
        char if char.isprintable() else char.encode("unicode_escape").decode("ascii")
        for char in value[:limit]
    )


def _menu_status() -> None:
    """Readable, read-only summary of the existing standalone status command."""
    from local_agent.cli.diagnostics import command_status

    output = io.StringIO()
    with redirect_stdout(output):
        code = command_status(argparse.Namespace())
    try:
        report = json.loads(output.getvalue())
    except (ValueError, TypeError):
        report = None
    if not isinstance(report, dict) or code != 0:
        print("Status: unverified. Use the standalone 'status' command for details.")
        return
    print("Status (last saved observation; not an execution readiness check)")
    print(f"  Installed version reported: {_safe_menu_text(report.get('daemon_version'))}")
    print(f"  Source revision reported: {_safe_menu_text(report.get('self_revision'), 64)}")
    alive = report.get("process_alive")
    if alive is True:
        process = "alive (heartbeat freshness not established here)"
    elif alive is False:
        process = "not running or unverified"
    else:
        process = "unverified"
    print(f"  Status owner process: {process}")
    print(f"  Last reported state: {_safe_menu_text(report.get('state'))}")
    print(f"  Last report timestamp: {_safe_menu_text(report.get('updated_at'), 64)}")
    print("  Use 5. Overview for age, worker evidence and durable records.")


def _menu_polling(args: argparse.Namespace) -> None:
    """Readable feedback while delegating all writes to the existing CLI."""
    output = io.StringIO()
    with redirect_stdout(output):
        code = command_polling(args)
    try:
        result = json.loads(output.getvalue())
    except (ValueError, TypeError):
        result = None
    if not isinstance(result, dict) or code != 0 or result.get("ok") is not True:
        print("Polling: unavailable or change rejected; existing settings not confirmed.")
        print("  Details: python -m local_agent.cli.diagnostics polling show")
        return
    mode = result.get("mode")
    if mode == "adaptive":
        description = "Adaptive (2/5/15 seconds after activity)"
    elif mode == "fixed" and type(result.get("seconds")) is int:
        description = f"Fixed ({result['seconds']} seconds)"
    else:
        print("Polling: settings unverified.")
        return
    change = getattr(args, "polling_command", "show") != "show"
    print(f"Polling {'preference saved' if change else 'preference'}: {description}")
    print("  Applies to host, control and project Git checks.")
    print("  A saved preference is reloaded automatically; this is not a live service check.")



def _polling_menu(state_dir: Path | None) -> None:
    """Keep each 0 Back action within the nearest CLI menu level."""
    while True:
        print("\nPolling")
        print("1. Show current settings")
        print("2. Adaptive (2/5/15 s)")
        print("3. Fixed interval")
        print("\n0. Back")
        choice = input("> ").strip()
        if choice == "0":
            return
        if choice == "1":
            _menu_polling(argparse.Namespace(
                polling_command="show", state_dir=state_dir,
            ))
        elif choice == "2":
            _menu_polling(argparse.Namespace(
                polling_command="adaptive", state_dir=state_dir,
            ))
        elif choice == "3":
            raw = input("Polling interval in seconds (2..300; 0 = Back): ").strip()
            if raw == "0":
                continue
            try:
                seconds = int(raw)
            except ValueError:
                print("Enter a whole number of seconds, or 0 to go back.")
                continue
            _menu_polling(argparse.Namespace(
                polling_command="fixed", seconds=seconds, state_dir=state_dir,
            ))
        else:
            print("Unknown option.")



def command_menu(args: argparse.Namespace) -> int:
    """Small built-in terminal menu, never another UI scheduler."""
    actions = ("Status", "Doctor", "Polling", "Repositories", "Overview", "Exit")
    if args.list:
        for index, action in enumerate(actions, 1):
            print(f"{0 if action == 'Exit' else index}. {action}")
        return 0
    try:
        import sys
        if not sys.stdin.isatty():
            print("Interactive menu requires a terminal; use 'menu --list' or 'polling --help'.")
            return 2
        while True:
            print("\nLocal Agent")
            print("1. Status\n2. Doctor\n3. Polling\n4. Repositories\n"
                  "5. Overview\n\n0. Exit")
            choice = input("> ").strip()
            if choice == "0":
                return 0
            if choice == "1":
                _menu_status()
            elif choice == "2":
                _menu_doctor()
            elif choice == "3":
                _polling_menu(getattr(args, "state_dir", None))
            elif choice == "4":
                from local_agent.cli.repositories import command_repositories_menu
                command_repositories_menu(getattr(args, "registry_file", None))
            elif choice == "5":
                from local_agent.cli.overview import command_overview

                state_root = getattr(args, "state_dir", None)
                command_overview(argparse.Namespace(
                    status_file=state_root / "status.json" if state_root is not None else None,
                    registry_file=getattr(args, "registry_file", None),
                    state_root=state_root,
                    max_age_seconds=180,
                    with_host_resources=False,
                    human=True,
                ))
            else:
                print("Unknown option.")
    except (EOFError, KeyboardInterrupt):
        print()
        return 0


def register_menu_parser(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser("menu", help="interactive terminal status/doctor/polling/repositories/overview menu")
    parser.add_argument("--list", action="store_true", help="list menu entries without interaction")
    parser.add_argument("--state-dir", type=Path, help="private state (offline tests)")
    parser.add_argument("--registry-file", type=Path, help="local project registry for menu")
    parser.set_defaults(func=command_menu)
