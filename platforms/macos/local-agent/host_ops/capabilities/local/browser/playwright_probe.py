"""Internal Playwright worker for one bounded managed-browser navigation."""

from __future__ import annotations

import argparse
import importlib
import json
import os
from collections.abc import Sequence
from typing import Any

from .managed import (
    _PROBE_URL_ENV,
    SUPPORTED_BROWSER_ENGINES,
    ManagedBrowserError,
    _bounded_title,
    _redact_probe_error,
    _sanitize_public_url,
)
from .models import ManagedBrowserProbe


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--engine", choices=SUPPORTED_BROWSER_ENGINES, required=True)
    parser.add_argument("--timeout", type=float, required=True, dest="timeout_seconds")
    return parser


def _load_playwright_sync_api() -> Any:
    try:
        return importlib.import_module("playwright.sync_api")
    except ModuleNotFoundError as exc:
        if exc.name == "playwright" or str(exc.name).startswith("playwright."):
            raise ManagedBrowserError(
                "Playwright is not installed; install the optional 'host-ops[browser]' extra"
            ) from exc
        raise


def _operation_timeout_ms(timeout_seconds: float) -> float:
    cleanup_reserve = min(1.0, timeout_seconds / 4.0)
    return max(250.0, (timeout_seconds - cleanup_reserve) * 1000.0)


def _run_probe(url: str, engine: str, timeout_seconds: float) -> ManagedBrowserProbe:
    sync_api = _load_playwright_sync_api()
    timeout_ms = _operation_timeout_ms(timeout_seconds)
    counts = {"console": 0, "page": 0, "request": 0}

    def on_console(message: Any) -> None:
        if getattr(message, "type", None) == "error":
            counts["console"] += 1

    def on_page_error(_error: Any) -> None:
        counts["page"] += 1

    def on_request_failed(_request: Any) -> None:
        counts["request"] += 1

    with sync_api.sync_playwright() as playwright:
        browser_type = getattr(playwright, engine)
        browser = browser_type.launch(headless=True, timeout=timeout_ms)
        try:
            context = browser.new_context(accept_downloads=False)
            try:
                context.set_default_timeout(timeout_ms)
                context.set_default_navigation_timeout(timeout_ms)
                page = context.new_page()
                page.on("console", on_console)
                page.on("pageerror", on_page_error)
                page.on("requestfailed", on_request_failed)
                response = page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
                status = getattr(response, "status", None) if response is not None else None
                final_url = _sanitize_public_url(str(page.url))
                if not final_url:
                    raise ManagedBrowserError(
                        "managed browser probe ended on a non-HTTP(S) final URL"
                    )
                return ManagedBrowserProbe(
                    engine=engine,
                    requested_url=_sanitize_public_url(url),
                    final_url=final_url,
                    title=_bounded_title(page.title()),
                    status_code=status if isinstance(status, int) else None,
                    console_error_count=counts["console"],
                    page_error_count=counts["page"],
                    request_failure_count=counts["request"],
                )
            finally:
                context.close()
        finally:
            browser.close()


def _error_message(exc: Exception, *, engine: str, url: str) -> str:
    detail = _redact_probe_error(str(exc), url)
    if "Executable doesn't exist" in detail or "browser executable" in detail.lower():
        return (
            f"{engine} browser binary is unavailable; install it explicitly with "
            f"'python -m playwright install {engine}'"
        )
    return detail or type(exc).__name__


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    url = os.environ.get(_PROBE_URL_ENV, "")
    if not url:
        print(json.dumps({"error": "managed browser probe URL is missing"}, sort_keys=True))
        return 2
    try:
        evidence = _run_probe(url, args.engine, args.timeout_seconds)
    except Exception as exc:
        print(
            json.dumps(
                {"error": _error_message(exc, engine=args.engine, url=url)},
                sort_keys=True,
            )
        )
        return 1
    print(json.dumps(evidence.as_dict(), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
