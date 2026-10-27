#!/usr/bin/env python3
"""Control one dedicated Chat Bridge Chromium profile through Host Ops.

Normal start/status/stop use the managed loopback-CDP session and load the unpacked
Bridge extension. login-start/login-status/login-finish are a thin wrapper around Host
Ops interactive mode for user-driven authentication on the same owned profile. The
interactive mode deliberately has no CDP and no extension flags; login-finish also
clears only tab/session restore state so stale authentication tabs are not reopened
when managed mode starts again.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

ROOT = Path(__file__).resolve().parents[1]
BRIDGE_DIR = ROOT / "chat_bridge"
_ALLOWED_CHAT_HOSTS = frozenset({"chatgpt.com", "chat.openai.com"})
_ACTIVE_STREAM_SELECTOR = (
    'button[data-testid="stop-button"], button[data-testid="composer-stop-button"]'
)
_MAX_CAPTURE_CHARS = 1_048_576
_BRANDED_MAC_CHROME = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")


class BridgeHostOpsSessionError(RuntimeError):
    """Raised when the dedicated Chat Bridge browser cannot be controlled safely."""


def normalize_start_url(raw: str) -> str:
    value = raw.strip()
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise BridgeHostOpsSessionError("start URL is invalid") from exc
    hostname = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or hostname not in _ALLOWED_CHAT_HOSTS:
        raise BridgeHostOpsSessionError("start URL must use an approved ChatGPT HTTPS host")
    if parsed.username is not None or parsed.password is not None or port is not None:
        raise BridgeHostOpsSessionError("start URL must not contain credentials or a port")
    if parsed.query or parsed.fragment:
        raise BridgeHostOpsSessionError("start URL must not contain query or fragment data")
    path = parsed.path or "/"
    return urlunsplit(("https", hostname, path, "", ""))


def _resolve_hostops(raw: str) -> str:
    if os.sep in raw:
        path = Path(raw).expanduser()
        if not path.is_file() or not os.access(path, os.X_OK):
            raise BridgeHostOpsSessionError("explicit Host Ops executable is not executable")
        return str(path)
    resolved = shutil.which(raw)
    if resolved is None:
        raise BridgeHostOpsSessionError("Host Ops executable was not found on PATH")
    return resolved


def _validate_bridge_dir(bridge_dir: Path) -> str:
    resolved = bridge_dir.resolve()
    if not resolved.is_dir() or not (resolved / "manifest.json").is_file():
        raise BridgeHostOpsSessionError("Chat Bridge extension directory is incomplete")
    return str(resolved)


def _validate_bridge_browser_executable(raw: str) -> str:
    value = raw.strip()
    if not value:
        raise BridgeHostOpsSessionError("browser executable is required")
    path = Path(value).expanduser()
    resolved = path.resolve(strict=False)
    if resolved == _BRANDED_MAC_CHROME.resolve(strict=False):
        raise BridgeHostOpsSessionError(
            "branded Google Chrome is not allowed for the dedicated Chat Bridge profile; "
            "use Chrome for Testing or Chromium"
        )
    return str(path)


def _validate_timeout(value: float) -> float:
    if not 1 <= value <= 120:
        raise BridgeHostOpsSessionError("timeout must be at least 1 and at most 120 seconds")
    return float(value)


def _run_hostops_json(
    argv: Sequence[str],
    *,
    timeout_seconds: float,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> dict[str, Any]:
    try:
        completed = runner(
            list(argv),
            text=True,
            capture_output=True,
            timeout=timeout_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise BridgeHostOpsSessionError("Host Ops command exceeded the operator timeout") from exc
    stdout = completed.stdout or ""
    stderr = completed.stderr or ""
    if len(stdout) > _MAX_CAPTURE_CHARS or len(stderr) > _MAX_CAPTURE_CHARS:
        raise BridgeHostOpsSessionError("Host Ops command output exceeded its bound")
    if completed.returncode != 0:
        detail = stderr.strip()[:512]
        suffix = f": {detail}" if detail else ""
        raise BridgeHostOpsSessionError(
            f"Host Ops command failed with exit {completed.returncode}{suffix}"
        )
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise BridgeHostOpsSessionError("Host Ops command returned invalid JSON") from exc
    if not isinstance(payload, dict):
        raise BridgeHostOpsSessionError("Host Ops command returned a non-object JSON value")
    return payload


def _validate_session_payload(payload: dict[str, Any], *, action: str) -> None:
    state = payload.get("state")
    allowed_states = {
        "start": {"running"},
        "status": {"running", "stopped", "unhealthy"},
        "stop": {"stopped"},
        "login-start": {"running"},
        "login-status": {"running", "stopped"},
        "login-finish": {"stopped"},
    }[action]
    if state not in allowed_states:
        raise BridgeHostOpsSessionError(f"Host Ops returned an invalid {action} session state")
    endpoint = payload.get("endpoint")
    if action in {"login-start", "login-status", "login-finish"}:
        if endpoint is not None:
            raise BridgeHostOpsSessionError("interactive login session unexpectedly exposed CDP")
        return
    if state == "running":
        if not isinstance(endpoint, str) or not endpoint.startswith("http://127.0.0.1:"):
            raise BridgeHostOpsSessionError("running session did not return a loopback CDP endpoint")
    elif endpoint is not None and not isinstance(endpoint, str):
        raise BridgeHostOpsSessionError("Host Ops returned an invalid endpoint value")


def _chat_page_targets(payload: dict[str, Any]) -> tuple[str, ...]:
    targets = payload.get("targets")
    if not isinstance(targets, list):
        raise BridgeHostOpsSessionError("Host Ops inspect returned invalid target evidence")
    result: list[str] = []
    for target in targets:
        if not isinstance(target, dict) or target.get("type") != "page":
            continue
        raw_url = target.get("url")
        if not isinstance(raw_url, str):
            continue
        try:
            parsed = urlsplit(raw_url)
        except ValueError:
            continue
        if parsed.scheme != "https" or (parsed.hostname or "").lower() not in _ALLOWED_CHAT_HOSTS:
            continue
        target_id = target.get("id")
        if not isinstance(target_id, str) or not target_id:
            raise BridgeHostOpsSessionError("ChatGPT page target is missing its exact target id")
        result.append(target_id)
    return tuple(result)


def _active_stream_control_count(
    *,
    hostops: str,
    endpoint: str,
    target_id: str,
    timeout_seconds: float,
    runner: Callable[..., subprocess.CompletedProcess[str]],
) -> int:
    payload = _run_hostops_json(
        [
            hostops,
            "browser",
            "attach",
            "selectors",
            "--endpoint",
            endpoint,
            "--target-id",
            target_id,
            "--selector",
            _ACTIVE_STREAM_SELECTOR,
            "--timeout",
            f"{timeout_seconds:g}",
            "--json",
        ],
        timeout_seconds=timeout_seconds + 5,
        runner=runner,
    )
    if payload.get("target_id") != target_id or payload.get("target_type") != "page":
        raise BridgeHostOpsSessionError("active-stream preflight returned wrong target evidence")
    selectors = payload.get("selectors")
    if not isinstance(selectors, list) or len(selectors) != 1:
        raise BridgeHostOpsSessionError("active-stream preflight returned invalid selector evidence")
    item = selectors[0]
    count = item.get("match_count") if isinstance(item, dict) else None
    if (
        not isinstance(item, dict)
        or item.get("selector") != _ACTIVE_STREAM_SELECTOR
        or not isinstance(count, int)
        or isinstance(count, bool)
        or count < 0
    ):
        raise BridgeHostOpsSessionError("active-stream preflight returned invalid selector evidence")
    return count


def _guard_managed_stop(
    *,
    hostops: str,
    profile_dir: str,
    timeout_seconds: float,
    runner: Callable[..., subprocess.CompletedProcess[str]],
) -> None:
    status = _run_hostops_json(
        [
            hostops,
            "browser",
            "session",
            "status",
            "--profile-dir",
            profile_dir,
            "--timeout",
            f"{timeout_seconds:g}",
            "--json",
        ],
        timeout_seconds=timeout_seconds + 5,
        runner=runner,
    )
    _validate_session_payload(status, action="status")
    if status.get("state") != "running":
        return
    endpoint = status.get("endpoint")
    assert isinstance(endpoint, str)
    inspect = _run_hostops_json(
        [
            hostops,
            "browser",
            "attach",
            "inspect",
            "--endpoint",
            endpoint,
            "--timeout",
            f"{timeout_seconds:g}",
            "--json",
        ],
        timeout_seconds=timeout_seconds + 5,
        runner=runner,
    )
    for target_id in _chat_page_targets(inspect):
        if _active_stream_control_count(
            hostops=hostops,
            endpoint=endpoint,
            target_id=target_id,
            timeout_seconds=timeout_seconds,
            runner=runner,
        ):
            raise BridgeHostOpsSessionError(
                "refusing to stop the managed Chat Bridge browser while ChatGPT generation "
                "control is present; wait for the response to finish or use --force"
            )


def managed_bridge_session(
    *,
    action: str,
    hostops: str,
    profile_dir: str,
    timeout_seconds: float,
    browser_executable: str | None = None,
    start_url: str = "https://chatgpt.com/",
    bridge_dir: Path = BRIDGE_DIR,
    force: bool = False,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> dict[str, Any]:
    timeout = _validate_timeout(timeout_seconds)
    if action not in {
        "start",
        "status",
        "stop",
        "login-start",
        "login-status",
        "login-finish",
    }:
        raise BridgeHostOpsSessionError("unsupported managed browser action")
    if force and action != "stop":
        raise BridgeHostOpsSessionError("force is supported only for managed stop")
    if action == "stop" and not force:
        _guard_managed_stop(
            hostops=hostops,
            profile_dir=profile_dir,
            timeout_seconds=timeout,
            runner=runner,
        )

    hostops_action = {
        "login-start": "interactive-start",
        "login-status": "interactive-status",
        "login-finish": "interactive-stop",
    }.get(action, action)
    command = [hostops, "browser", "session", hostops_action, "--profile-dir", profile_dir]
    extension_dir: str | None = None
    normalized_url: str | None = None

    if action in {"start", "login-start"}:
        if not browser_executable:
            raise BridgeHostOpsSessionError("browser executable is required for start")
        browser = _validate_bridge_browser_executable(browser_executable)
        normalized_url = normalize_start_url(start_url)
        command.extend(["--browser-executable", browser, "--url", normalized_url])
        if action == "start":
            extension_dir = _validate_bridge_dir(bridge_dir)
            command.extend(["--extension-dir", extension_dir])
    elif action == "login-finish":
        command.append("--clear-session-restore")

    command.extend(["--timeout", f"{timeout:g}", "--json"])
    payload = _run_hostops_json(
        command,
        timeout_seconds=timeout + 5,
        runner=runner,
    )
    _validate_session_payload(payload, action=action)
    result: dict[str, Any] = {
        "action": action,
        "profile_dir": profile_dir,
        "result": payload,
    }
    if extension_dir is not None:
        result["bridge_dir"] = extension_dir
    if normalized_url is not None:
        result["start_url"] = normalized_url
    return result


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--hostops",
        default="hostops",
        help="Host Ops executable name or explicit path (default: hostops)",
    )
    subparsers = parser.add_subparsers(dest="action", required=True)

    def add_start(name: str, help_text: str) -> None:
        command = subparsers.add_parser(name, help=help_text)
        command.add_argument("--profile-dir", required=True)
        command.add_argument("--browser-executable", required=True)
        command.add_argument("--url", default="https://chatgpt.com/", dest="start_url")
        command.add_argument("--timeout", type=float, default=20.0, dest="timeout_seconds")

    add_start("start", "start one isolated managed Chat Bridge Chromium session")
    add_start(
        "login-start",
        "start the same owned profile without CDP or extension flags for manual authentication",
    )

    status = subparsers.add_parser("status", help="inspect one exact managed Chat Bridge profile")
    status.add_argument("--profile-dir", required=True)
    status.add_argument("--timeout", type=float, default=5.0, dest="timeout_seconds")

    login_status = subparsers.add_parser(
        "login-status", help="inspect the exact interactive login profile"
    )
    login_status.add_argument("--profile-dir", required=True)
    login_status.add_argument("--timeout", type=float, default=5.0, dest="timeout_seconds")

    stop = subparsers.add_parser("stop", help="stop only the exact managed Chat Bridge browser")
    stop.add_argument("--profile-dir", required=True)
    stop.add_argument("--timeout", type=float, default=10.0, dest="timeout_seconds")
    stop.add_argument(
        "--force",
        action="store_true",
        help="stop even when a ChatGPT generation control is present",
    )

    login_finish = subparsers.add_parser(
        "login-finish",
        help="stop the exact interactive login browser and discard only stale tab restore state",
    )
    login_finish.add_argument("--profile-dir", required=True)
    login_finish.add_argument("--timeout", type=float, default=10.0, dest="timeout_seconds")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    try:
        args = parse_args(argv)
        payload = managed_bridge_session(
            action=args.action,
            hostops=_resolve_hostops(args.hostops),
            profile_dir=args.profile_dir,
            browser_executable=getattr(args, "browser_executable", None),
            start_url=getattr(args, "start_url", "https://chatgpt.com/"),
            timeout_seconds=args.timeout_seconds,
            force=getattr(args, "force", False),
        )
    except BridgeHostOpsSessionError as exc:
        print(json.dumps({"error": str(exc)}, sort_keys=True), file=sys.stderr)
        return 1
    print(json.dumps(payload, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
