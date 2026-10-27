#!/usr/bin/env python3
"""Diagnose or recover one Chat Bridge content-script path through Host Ops.

This is an operator helper, not daemon/runtime behavior. It intentionally keeps the
extension's normal worker-owned self-heal as the primary path and uses Host Ops only
as an external fallback for one exact already-open ChatGPT conversation target.
"""

from __future__ import annotations

import argparse
import hashlib
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
_CHAT_MATCH_PATTERNS = frozenset({"https://chatgpt.com/*", "https://chat.openai.com/*"})
_COMPOSER_SELECTOR = (
    '#prompt-textarea, [data-testid="prompt-textarea"], '
    'div.ProseMirror[contenteditable="true"]'
)
_ACTIVE_STREAM_SELECTOR = (
    'button[data-testid="stop-button"], button[data-testid="composer-stop-button"]'
)
_MAX_CAPTURE_CHARS = 1_048_576


class BridgeHostOpsRecoveryError(RuntimeError):
    """Raised when external Bridge diagnosis/recovery cannot fail closed safely."""


def normalize_conversation_url(raw: str) -> str:
    value = raw.strip()
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise BridgeHostOpsRecoveryError("conversation URL is invalid") from exc
    hostname = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or hostname not in _ALLOWED_CHAT_HOSTS:
        raise BridgeHostOpsRecoveryError("conversation URL must use an approved ChatGPT HTTPS host")
    if parsed.username is not None or parsed.password is not None or port is not None:
        raise BridgeHostOpsRecoveryError("conversation URL must not contain credentials or a port")
    if parsed.query or parsed.fragment:
        raise BridgeHostOpsRecoveryError("conversation URL must not contain query or fragment data")
    if not parsed.path or parsed.path == "/":
        raise BridgeHostOpsRecoveryError("conversation URL must identify one concrete conversation path")
    return urlunsplit(("https", hostname, parsed.path, "", ""))


def bridge_content_script_fingerprints(
    bridge_dir: Path = BRIDGE_DIR,
) -> tuple[tuple[str, str], ...]:
    manifest_path = bridge_dir / "manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BridgeHostOpsRecoveryError("Bridge manifest could not be read") from exc
    entries = manifest.get("content_scripts")
    if not isinstance(entries, list):
        raise BridgeHostOpsRecoveryError("Bridge manifest content_scripts is invalid")

    scripts: list[str] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        matches = entry.get("matches")
        javascript = entry.get("js")
        if not isinstance(matches, list) or not isinstance(javascript, list):
            continue
        if not _CHAT_MATCH_PATTERNS.intersection(str(item) for item in matches):
            continue
        scripts.extend(str(item) for item in javascript)
    if not scripts:
        raise BridgeHostOpsRecoveryError("Bridge manifest has no ChatGPT content scripts")

    fingerprints: list[tuple[str, str]] = []
    seen_names: set[str] = set()
    root = bridge_dir.resolve()
    for relative_name in scripts:
        relative = Path(relative_name)
        basename = relative.name
        if not basename or basename in {".", ".."} or not basename.endswith(".js"):
            raise BridgeHostOpsRecoveryError("Bridge content-script path is invalid")
        if basename in seen_names:
            raise BridgeHostOpsRecoveryError("Bridge content-script basenames must be unique")
        candidate = (bridge_dir / relative).resolve()
        try:
            candidate.relative_to(root)
        except ValueError as exc:
            raise BridgeHostOpsRecoveryError("Bridge content-script path escapes chat_bridge") from exc
        if not candidate.is_file():
            raise BridgeHostOpsRecoveryError(f"Bridge content script is missing: {basename}")
        seen_names.add(basename)
        fingerprints.append((basename, hashlib.sha256(candidate.read_bytes()).hexdigest()))
    return tuple(fingerprints)


def select_exact_page_target(payload: dict[str, Any], conversation_url: str) -> str:
    targets = payload.get("targets")
    if not isinstance(targets, list):
        raise BridgeHostOpsRecoveryError("Host Ops inspect returned invalid target evidence")
    matches = [
        target
        for target in targets
        if isinstance(target, dict)
        and target.get("type") == "page"
        and target.get("url") == conversation_url
        and isinstance(target.get("id"), str)
        and target.get("id")
    ]
    if not matches:
        raise BridgeHostOpsRecoveryError("no exact open page target matches the conversation URL")
    if len(matches) != 1:
        raise BridgeHostOpsRecoveryError("conversation URL matches multiple page targets; refusing ambiguity")
    return str(matches[0]["id"])


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
        raise BridgeHostOpsRecoveryError("Host Ops command exceeded the operator timeout") from exc
    stdout = completed.stdout or ""
    stderr = completed.stderr or ""
    if len(stdout) > _MAX_CAPTURE_CHARS or len(stderr) > _MAX_CAPTURE_CHARS:
        raise BridgeHostOpsRecoveryError("Host Ops command output exceeded its bound")
    if completed.returncode != 0:
        detail = stderr.strip()[:512]
        suffix = f": {detail}" if detail else ""
        raise BridgeHostOpsRecoveryError(
            f"Host Ops command failed with exit {completed.returncode}{suffix}"
        )
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise BridgeHostOpsRecoveryError("Host Ops command returned invalid JSON") from exc
    if not isinstance(payload, dict):
        raise BridgeHostOpsRecoveryError("Host Ops command returned a non-object JSON value")
    return payload


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
        raise BridgeHostOpsRecoveryError("active-stream preflight returned wrong target evidence")
    selectors = payload.get("selectors")
    if not isinstance(selectors, list) or len(selectors) != 1:
        raise BridgeHostOpsRecoveryError("active-stream preflight returned invalid selector evidence")
    item = selectors[0]
    count = item.get("match_count") if isinstance(item, dict) else None
    if (
        not isinstance(item, dict)
        or item.get("selector") != _ACTIVE_STREAM_SELECTOR
        or not isinstance(count, int)
        or isinstance(count, bool)
        or count < 0
    ):
        raise BridgeHostOpsRecoveryError("active-stream preflight returned invalid selector evidence")
    return count


def _validate_managed_loopback_endpoint(raw: Any) -> str:
    if not isinstance(raw, str):
        raise BridgeHostOpsRecoveryError(
            "managed Chat Bridge profile did not return a loopback CDP endpoint"
        )
    try:
        parsed = urlsplit(raw)
        port = parsed.port
    except ValueError as exc:
        raise BridgeHostOpsRecoveryError(
            "managed Chat Bridge profile did not return a loopback CDP endpoint"
        ) from exc
    if (
        parsed.scheme != "http"
        or parsed.hostname != "127.0.0.1"
        or parsed.username is not None
        or parsed.password is not None
        or port is None
        or not 1 <= port <= 65535
        or parsed.path
        or parsed.query
        or parsed.fragment
    ):
        raise BridgeHostOpsRecoveryError(
            "managed Chat Bridge profile did not return a loopback CDP endpoint"
        )
    return f"http://127.0.0.1:{port}"


def resolve_managed_profile_endpoint(
    *,
    hostops: str,
    profile_dir: str,
    timeout_seconds: float,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> str:
    """Resolve one running managed profile to its exact current loopback CDP endpoint."""
    if not 3 <= timeout_seconds <= 120:
        raise BridgeHostOpsRecoveryError("timeout must be at least 3 and at most 120 seconds")
    profile = profile_dir.strip()
    if not profile:
        raise BridgeHostOpsRecoveryError("managed Chat Bridge profile path must not be empty")
    payload = _run_hostops_json(
        [
            hostops,
            "browser",
            "session",
            "status",
            "--profile-dir",
            profile,
            "--timeout",
            f"{timeout_seconds:g}",
            "--json",
        ],
        timeout_seconds=timeout_seconds + 5,
        runner=runner,
    )
    if payload.get("state") != "running":
        raise BridgeHostOpsRecoveryError("managed Chat Bridge profile is not running")
    return _validate_managed_loopback_endpoint(payload.get("endpoint"))


def external_bridge_check(
    *,
    hostops: str,
    endpoint: str,
    conversation_url: str,
    recover: bool,
    timeout_seconds: float,
    force: bool = False,
    bridge_dir: Path = BRIDGE_DIR,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> dict[str, Any]:
    normalized_url = normalize_conversation_url(conversation_url)
    if not 3 <= timeout_seconds <= 120:
        raise BridgeHostOpsRecoveryError("timeout must be at least 3 and at most 120 seconds")

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
    target_id = select_exact_page_target(inspect, normalized_url)
    fingerprints = bridge_content_script_fingerprints(bridge_dir)

    if recover and not force:
        active_controls = _active_stream_control_count(
            hostops=hostops,
            endpoint=endpoint,
            target_id=target_id,
            timeout_seconds=timeout_seconds,
            runner=runner,
        )
        if active_controls:
            raise BridgeHostOpsRecoveryError(
                "refusing recovery while ChatGPT generation control is present; "
                "wait for the response to finish or use --force"
            )

    command = [
        hostops,
        "browser",
        "attach",
        "recover-content-script" if recover else "readiness",
        "--endpoint",
        endpoint,
        "--target-id",
        target_id,
    ]
    if recover:
        command.extend(["--expect-url", normalized_url])
    command.extend(["--selector", _COMPOSER_SELECTOR])
    for name, digest in fingerprints:
        command.extend(["--script-fingerprint", f"{name}={digest}"])
    command.extend(["--timeout", f"{timeout_seconds:g}", "--json"])
    result = _run_hostops_json(
        command,
        timeout_seconds=timeout_seconds + 5,
        runner=runner,
    )
    return {
        "mode": "recover" if recover else "inspect",
        "conversation_url": normalized_url,
        "target_id": target_id,
        "fingerprinted_scripts": [name for name, _digest in fingerprints],
        "result": result,
    }


def _resolve_hostops(raw: str) -> str:
    if os.sep in raw:
        path = Path(raw).expanduser()
        if not path.is_file() or not os.access(path, os.X_OK):
            raise BridgeHostOpsRecoveryError("explicit Host Ops executable is not executable")
        return str(path)
    resolved = shutil.which(raw)
    if resolved is None:
        raise BridgeHostOpsRecoveryError("Host Ops executable was not found on PATH")
    return resolved


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument(
        "--endpoint",
        help="explicit loopback Chromium CDP endpoint, for example http://127.0.0.1:9222",
    )
    target.add_argument(
        "--profile-dir",
        help="exact Host Ops managed browser profile whose current endpoint should be resolved",
    )
    parser.add_argument(
        "--conversation-url",
        required=True,
        help="exact sanitized ChatGPT conversation URL with no query or fragment",
    )
    parser.add_argument(
        "--recover",
        action="store_true",
        help="allow Host Ops bounded one-shot content-script recovery; default is read-only readiness",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="allow recovery even when a ChatGPT generation control is present",
    )
    parser.add_argument(
        "--hostops",
        default="hostops",
        help="Host Ops executable name or explicit path (default: hostops)",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=30.0,
        dest="timeout_seconds",
        help="bounded timeout for each Host Ops operation, 3-120 seconds (default: 30)",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    try:
        args = parse_args(argv)
        hostops = _resolve_hostops(args.hostops)
        if args.force and not args.recover:
            raise BridgeHostOpsRecoveryError("--force requires --recover")
        endpoint = args.endpoint
        if endpoint is None:
            endpoint = resolve_managed_profile_endpoint(
                hostops=hostops,
                profile_dir=args.profile_dir,
                timeout_seconds=args.timeout_seconds,
            )
        payload = external_bridge_check(
            hostops=hostops,
            endpoint=endpoint,
            conversation_url=args.conversation_url,
            recover=args.recover,
            timeout_seconds=args.timeout_seconds,
            force=args.force,
        )
    except BridgeHostOpsRecoveryError as exc:
        print(json.dumps({"error": str(exc)}, sort_keys=True), file=sys.stderr)
        return 1
    print(json.dumps(payload, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
