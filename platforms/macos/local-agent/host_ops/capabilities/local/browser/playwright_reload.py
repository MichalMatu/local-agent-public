"""Internal Playwright helper for one guarded exact-target Chromium reload."""

from __future__ import annotations

import argparse
import json
import os
from typing import Any
from urllib.parse import urlsplit

from .attach import _ATTACH_ENDPOINT_ENV, BrowserAttachError
from .inspection import _sanitize_target_url
from .models import BrowserReloadResult
from .playwright_snapshot import (
    _bounded_text,
    _load_playwright_sync_api,
    _operation_timeout_ms,
    _target_info,
)
from .reload import (
    _MAX_TITLE_CHARS,
    _RELOAD_EXPECTED_URL_ENV,
    BrowserReloadError,
    _normalize_expected_url,
)
from .snapshot import _SNAPSHOT_TARGET_ENV, BrowserSnapshotError


def _http_target_url(target_info: dict[str, Any]) -> str:
    value = _sanitize_target_url(str(target_info.get("url") or ""))
    try:
        parsed = urlsplit(value)
        _ = parsed.port
    except ValueError as exc:
        raise BrowserReloadError("browser target does not have a valid HTTP(S) URL") from exc
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise BrowserReloadError("browser target does not have a valid HTTP(S) URL")
    return value


def _run_reload(
    endpoint: str,
    target_id: str,
    expected_url: str,
    timeout_seconds: float,
) -> BrowserReloadResult:
    normalized_expected_url = _normalize_expected_url(expected_url)
    sync_api = _load_playwright_sync_api()
    timeout_ms = _operation_timeout_ms(timeout_seconds)

    with sync_api.sync_playwright() as playwright:
        browser = playwright.chromium.connect_over_cdp(
            endpoint,
            timeout=timeout_ms,
            is_local=True,
            no_defaults=True,
        )
        matched_session = None
        matched_page = None
        try:
            for context in browser.contexts:
                for page in context.pages:
                    session = context.new_cdp_session(page)
                    keep = False
                    try:
                        info = _target_info(session.send("Target.getTargetInfo"))
                        if info["targetId"] == target_id:
                            matched_session = session
                            matched_page = page
                            keep = True
                            break
                    finally:
                        if not keep:
                            session.detach()
                if matched_session is not None:
                    break

            if matched_session is None or matched_page is None:
                raise BrowserReloadError("requested browser target is not an attached page target")

            before = _target_info(matched_session.send("Target.getTargetInfo"))
            if before["targetId"] != target_id or before["type"] != "page":
                raise BrowserReloadError("requested browser target is not a page target")
            before_url = _http_target_url(before)
            if before_url != normalized_expected_url:
                raise BrowserReloadError("current browser target URL does not match expected URL")
            before_title = _bounded_text(before.get("title"), _MAX_TITLE_CHARS)

            with matched_page.expect_event("load", timeout=timeout_ms):
                matched_session.send("Page.reload")

            after = _target_info(matched_session.send("Target.getTargetInfo"))
            if after["targetId"] != target_id or after["type"] != "page":
                raise BrowserReloadError("browser target identity changed during reload")
            after_url = _http_target_url(after)
            after_title = _bounded_text(after.get("title"), _MAX_TITLE_CHARS)
            return BrowserReloadResult(
                endpoint=endpoint,
                target_id=target_id,
                target_type="page",
                expected_url=normalized_expected_url,
                before_url=before_url,
                before_title=before_title,
                after_url=after_url,
                after_title=after_title,
            )
        finally:
            try:
                if matched_session is not None:
                    matched_session.detach()
            finally:
                browser.close(reason="host-ops guarded page reload complete")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--timeout", type=float, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    endpoint = os.environ.get(_ATTACH_ENDPOINT_ENV, "").strip()
    target_id = os.environ.get(_SNAPSHOT_TARGET_ENV, "").strip()
    expected_url = os.environ.get(_RELOAD_EXPECTED_URL_ENV, "").strip()
    if not endpoint or not target_id or not expected_url:
        print(json.dumps({"error": "browser reload input is missing"}, sort_keys=True))
        return 2
    if not 1 <= args.timeout <= 120:
        print(json.dumps({"error": "browser reload timeout is invalid"}, sort_keys=True))
        return 2

    try:
        result = _run_reload(endpoint, target_id, expected_url, args.timeout)
    except (BrowserReloadError, BrowserAttachError, BrowserSnapshotError, ValueError) as exc:
        print(json.dumps({"error": str(exc)[:1000]}, sort_keys=True))
        return 1
    except Exception:
        print(json.dumps({"error": "browser reload failed"}, sort_keys=True))
        return 1

    print(json.dumps(result.as_dict(), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
