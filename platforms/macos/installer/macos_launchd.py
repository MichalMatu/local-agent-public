#!/usr/bin/env python3
"""Render, install and inspect the Local Agent macOS LaunchAgent."""

from __future__ import annotations

import argparse
import platform
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from local_agent.platform.macos_launchd import (  # noqa: E402
    LABEL,
    bootout,
    default_launch_agent_path,
    print_status,
    render_launch_agent,
    restart_launch_agent,
    validate_checkout,
    write_launch_agent,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command",
        choices=("render", "install", "restart", "status", "uninstall"),
    )
    parser.add_argument(
        "--mode",
        choices=("parallel", "multirepo", "single"),
    )
    parser.add_argument(
        "--repo-root",
        type=Path,
    )
    parser.add_argument("--home", type=Path, default=Path.home())
    parser.add_argument("--registry", type=Path)
    parser.add_argument("--max-workers", type=int)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--with-chat-bridge-pairing", action="store_true",
                        help="also activate independent pairing-only login sidecar (install only)")
    return parser


def _require_macos() -> None:
    if platform.system() != "Darwin":
        raise SystemExit("this command requires macOS")


def _render(args: argparse.Namespace) -> bytes:
    return render_launch_agent(
        args.mode,
        repo_root=args.repo_root,
        home=args.home,
        max_workers=args.max_workers,
        registry_path=args.registry,
    )


def main() -> int:
    parser = _parser()
    args = parser.parse_args()
    rendering = args.command in ("render", "install")
    if not rendering and any(value is not None for value in (
        args.mode, args.repo_root, args.registry, args.max_workers,
    )):
        parser.error("source, mode, registry and worker options apply only to render/install")
    if rendering:
        args.mode = args.mode or "parallel"
        args.repo_root = args.repo_root or REPO_ROOT
        args.max_workers = args.max_workers if args.max_workers is not None else 2
    if args.with_chat_bridge_pairing and args.command != "install":
        raise SystemExit("--with-chat-bridge-pairing is supported only with install")
    plist_path = args.output or default_launch_agent_path(args.home)

    if args.command == "render":
        content = _render(args)
        if args.output:
            write_launch_agent(plist_path, content)
            print(plist_path)
        else:
            sys.stdout.buffer.write(content)
        return 0

    _require_macos()

    if args.command == "status":
        result = print_status()
        if result.stdout:
            print(result.stdout, end="")
        if result.stderr:
            print(result.stderr, end="", file=sys.stderr)
        return result.returncode

    if args.command == "uninstall":
        bootout(check=False)
        plist_path.unlink(missing_ok=True)
        print(f"removed {plist_path}")
        return 0

    if args.command == "install":
        validate_checkout(args.repo_root)
        write_launch_agent(plist_path, _render(args))
        print(f"installed {plist_path}")
        if args.with_chat_bridge_pairing:
            from local_agent.platform.macos_bridge_pairing import setup_pairing_service

            try:
                pairing_status = setup_pairing_service(repo_root=args.repo_root, home=args.home)
            except (OSError, RuntimeError, ValueError) as exc:
                raise SystemExit(f"pairing sidecar not activated: {exc}") from None
            print(f"Chat Bridge pairing sidecar: {pairing_status} (no GitHub intake/Send)")
        print(
            f"not activated; run: {sys.executable} scripts/macos_launchd.py restart"
        )
        return 0

    if args.command == "restart":
        # Restart the installed definition. Rendering from this invocation's
        # checkout silently downgrades independently installed releases.
        if not plist_path.is_file() or plist_path.is_symlink():
            raise SystemExit("installed LaunchAgent missing or symlinked; install explicitly")
        try:
            restart_launch_agent(plist_path)
        except subprocess.CalledProcessError as exc:
            if exc.stdout:
                print(exc.stdout, end="")
            if exc.stderr:
                print(exc.stderr, end="", file=sys.stderr)
            return int(exc.returncode)
        except TimeoutError as exc:
            print(str(exc), file=sys.stderr)
            return 1
        print(f"restarted {LABEL} from {plist_path}")
        return 0

    raise AssertionError(f"unhandled command: {args.command}")


if __name__ == "__main__":
    raise SystemExit(main())
