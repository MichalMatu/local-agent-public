"""Internal Playwright helper for read-only Chromium CDP attachment."""

from __future__ import annotations

import argparse
import importlib
import json
import os
import re
from typing import Any

from .attach import _ATTACH_ENDPOINT_ENV, BrowserAttachError
from .inspection import _sanitize_target_url
from .models import BrowserAttachInspection, BrowserAttachTarget

_MAX_TARGET_ID_CHARS = 256
_MAX_TARGET_TYPE_CHARS = 64
_MAX_TITLE_CHARS = 512
_MAX_BROWSER_VERSION_CHARS = 128
_URL_IN_ERROR_RE = re.compile(r"(?:https?|wss?)://[^\s\"'<>]+")


def _load_playwright_sync_api() -> Any:
    try:
        return importlib.import_module("playwright.sync_api")
    except ModuleNotFoundError as exc:
        if exc.name == "playwright" or str(exc.name).startswith("playwright."):
            raise BrowserAttachError(
                "Playwright is not installed; install the optional 'host-ops[browser]' extra"
            ) from exc
        raise


def _run_attach(endpoint: str, timeout_seconds: float) -> BrowserAttachInspection:
    sync_api = _load_playwright_sync_api()
    operation_timeout_ms = max((timeout_seconds - 1.0) * 1000.0, 250.0)

    with sync_api.sync_playwright() as playwright:
        browser = playwright.chromium.connect_over_cdp(
            endpoint,
            timeout=operation_timeout_ms,
            is_local=True,
            no_defaults=True,
        )
        session = None
        try:
            browser_version = str(browser.version)[:_MAX_BROWSER_VERSION_CHARS]
            context_count = len(browser.contexts)
            session = browser.new_browser_cdp_session()
            response = session.send("Target.getTargets")
            targets = _targets_from_response(response)
            return BrowserAttachInspection(
                endpoint=endpoint,
                browser_version=browser_version,
                context_count=context_count,
                targets=targets,
            )
        finally:
            try:
                if session is not None:
                    session.detach()
            finally:
                browser.close(reason="host-ops read-only CDP attach complete")


def _targets_from_response(response: object) -> tuple[BrowserAttachTarget, ...]:
    if not isinstance(response, dict):
        raise BrowserAttachError("CDP Target.getTargets returned a non-object response")
    raw_targets = response.get("targetInfos")
    if not isinstance(raw_targets, list):
        raise BrowserAttachError("CDP Target.getTargets returned an invalid target list")

    targets = tuple(_target_from_info(raw) for raw in raw_targets)
    return tuple(sorted(targets, key=lambda target: (target.target_type, target.target_id)))


def _target_from_info(raw: object) -> BrowserAttachTarget:
    if not isinstance(raw, dict):
        raise BrowserAttachError("CDP Target.getTargets returned an invalid target")
    target_id = raw.get("targetId")
    target_type = raw.get("type")
    title = raw.get("title")
    url = raw.get("url")
    attached = raw.get("attached")

    if not isinstance(target_id, str) or not 1 <= len(target_id) <= _MAX_TARGET_ID_CHARS:
        raise BrowserAttachError("CDP target has an invalid id")
    if not isinstance(target_type, str) or not 1 <= len(target_type) <= _MAX_TARGET_TYPE_CHARS:
        raise BrowserAttachError("CDP target has an invalid type")
    if not isinstance(attached, bool):
        raise BrowserAttachError("CDP target has an invalid attached state")

    title_text = title if isinstance(title, str) else str(title or "")
    raw_url = url if isinstance(url, str) else str(url or "")
    return BrowserAttachTarget(
        target_id=target_id,
        target_type=target_type,
        title=title_text[:_MAX_TITLE_CHARS],
        url=_sanitize_target_url(raw_url),
        attached=attached,
    )


def _error_message(exc: Exception) -> str:
    if isinstance(exc, BrowserAttachError):
        return str(exc)[:1000]

    detail = str(exc)

    def redact(match: re.Match[str]) -> str:
        value = match.group(0)
        if value.startswith(("ws://", "wss://")):
            return "<redacted-cdp-url>"
        return "<redacted-url>"

    return _URL_IN_ERROR_RE.sub(redact, detail)[:1000]


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--timeout", type=float, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    endpoint = os.environ.get(_ATTACH_ENDPOINT_ENV, "").strip()
    if not endpoint:
        print(json.dumps({"error": "browser CDP attach endpoint is missing"}, sort_keys=True))
        return 2
    if not 1 <= args.timeout <= 120:
        print(json.dumps({"error": "browser CDP attach timeout is invalid"}, sort_keys=True))
        return 2

    try:
        result = _run_attach(endpoint, args.timeout)
    except Exception as exc:
        print(json.dumps({"error": _error_message(exc)}, sort_keys=True))
        return 1

    print(json.dumps(result.as_dict(), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
