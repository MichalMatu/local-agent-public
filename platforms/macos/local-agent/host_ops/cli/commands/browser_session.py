"""CLI rendering and dispatch for persistent managed browser sessions."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence

from local_agent.host_ops.capabilities.local.browser import (
    InteractiveBrowserSessionController,
    InteractiveBrowserSessionError,
    ManagedBrowserSession,
    ManagedBrowserSessionController,
    ManagedBrowserSessionError,
)


def _render_session(result: ManagedBrowserSession, *, as_json: bool) -> int:
    if as_json:
        print(json.dumps(result.as_dict(), sort_keys=True))
        return 0
    parts = [f"state={result.state}"]
    if result.pid is not None:
        parts.append(f"pid={result.pid}")
    if result.endpoint is not None:
        parts.append(f"endpoint={result.endpoint}")
    if result.browser is not None:
        parts.append(f"browser={result.browser}")
    print(" ".join(parts))
    return 0


def run_session_start(
    profile_dir: str,
    browser_executable: str,
    *,
    extension_dirs: Sequence[str],
    start_url: str | None,
    timeout_seconds: float,
    as_json: bool,
    download_dir: str | None = None,
) -> int:
    try:
        controller = ManagedBrowserSessionController()
        if download_dir is None:
            result = controller.start(
                profile_dir,
                browser_executable,
                extension_dirs=extension_dirs,
                start_url=start_url,
                timeout_seconds=timeout_seconds,
            )
        else:
            result = controller.start(
                profile_dir,
                browser_executable,
                extension_dirs=extension_dirs,
                start_url=start_url,
                download_dir=download_dir,
                timeout_seconds=timeout_seconds,
            )
    except (ManagedBrowserSessionError, ValueError) as exc:
        if as_json:
            print(json.dumps({"error": str(exc)}, sort_keys=True), file=sys.stderr)
        else:
            print(f"browser session start failed: {exc}", file=sys.stderr)
        return 1
    return _render_session(result, as_json=as_json)


def run_session_status(
    profile_dir: str,
    *,
    timeout_seconds: float,
    as_json: bool,
) -> int:
    try:
        result = ManagedBrowserSessionController().status(
            profile_dir,
            timeout_seconds=timeout_seconds,
        )
    except (ManagedBrowserSessionError, ValueError) as exc:
        if as_json:
            print(json.dumps({"error": str(exc)}, sort_keys=True), file=sys.stderr)
        else:
            print(f"browser session status failed: {exc}", file=sys.stderr)
        return 1
    return _render_session(result, as_json=as_json)


def run_session_stop(
    profile_dir: str,
    *,
    timeout_seconds: float,
    as_json: bool,
) -> int:
    try:
        result = ManagedBrowserSessionController().stop(
            profile_dir,
            timeout_seconds=timeout_seconds,
        )
    except (ManagedBrowserSessionError, ValueError) as exc:
        if as_json:
            print(json.dumps({"error": str(exc)}, sort_keys=True), file=sys.stderr)
        else:
            print(f"browser session stop failed: {exc}", file=sys.stderr)
        return 1
    return _render_session(result, as_json=as_json)


def run_interactive_start(
    profile_dir: str,
    browser_executable: str,
    *,
    start_url: str | None,
    timeout_seconds: float,
    as_json: bool,
) -> int:
    try:
        result = InteractiveBrowserSessionController().start(
            profile_dir,
            browser_executable,
            start_url=start_url,
            timeout_seconds=timeout_seconds,
        )
    except (InteractiveBrowserSessionError, ValueError) as exc:
        if as_json:
            print(json.dumps({"error": str(exc)}, sort_keys=True), file=sys.stderr)
        else:
            print(f"interactive browser start failed: {exc}", file=sys.stderr)
        return 1
    return _render_session(result, as_json=as_json)


def run_interactive_status(
    profile_dir: str,
    *,
    timeout_seconds: float,
    as_json: bool,
) -> int:
    try:
        result = InteractiveBrowserSessionController().status(
            profile_dir,
            timeout_seconds=timeout_seconds,
        )
    except (InteractiveBrowserSessionError, ValueError) as exc:
        if as_json:
            print(json.dumps({"error": str(exc)}, sort_keys=True), file=sys.stderr)
        else:
            print(f"interactive browser status failed: {exc}", file=sys.stderr)
        return 1
    return _render_session(result, as_json=as_json)


def run_interactive_stop(
    profile_dir: str,
    *,
    clear_session_restore: bool,
    timeout_seconds: float,
    as_json: bool,
) -> int:
    try:
        result = InteractiveBrowserSessionController().stop(
            profile_dir,
            clear_session_restore=clear_session_restore,
            timeout_seconds=timeout_seconds,
        )
    except (InteractiveBrowserSessionError, ValueError) as exc:
        if as_json:
            print(json.dumps({"error": str(exc)}, sort_keys=True), file=sys.stderr)
        else:
            print(f"interactive browser stop failed: {exc}", file=sys.stderr)
        return 1
    return _render_session(result, as_json=as_json)


def run_browser_session(args: argparse.Namespace) -> int:
    if args.browser_session_command == "start":
        return run_session_start(
            args.profile_dir,
            args.browser_executable,
            extension_dirs=args.extension_dirs,
            start_url=args.start_url,
            timeout_seconds=args.timeout_seconds,
            as_json=args.as_json,
            download_dir=args.download_dir,
        )
    if args.browser_session_command == "status":
        return run_session_status(
            args.profile_dir,
            timeout_seconds=args.timeout_seconds,
            as_json=args.as_json,
        )
    if args.browser_session_command == "stop":
        return run_session_stop(
            args.profile_dir,
            timeout_seconds=args.timeout_seconds,
            as_json=args.as_json,
        )
    if args.browser_session_command == "interactive-start":
        return run_interactive_start(
            args.profile_dir,
            args.browser_executable,
            start_url=args.start_url,
            timeout_seconds=args.timeout_seconds,
            as_json=args.as_json,
        )
    if args.browser_session_command == "interactive-status":
        return run_interactive_status(
            args.profile_dir,
            timeout_seconds=args.timeout_seconds,
            as_json=args.as_json,
        )
    if args.browser_session_command == "interactive-stop":
        return run_interactive_stop(
            args.profile_dir,
            clear_session_restore=args.clear_session_restore,
            timeout_seconds=args.timeout_seconds,
            as_json=args.as_json,
        )
    raise ValueError(f"unsupported browser session command: {args.browser_session_command!r}")
