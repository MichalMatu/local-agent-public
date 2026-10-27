"""Internal Playwright helper for read-only CSS selector match counts."""

from __future__ import annotations

import argparse
import json
import os
from typing import Any, cast

from .attach import _ATTACH_ENDPOINT_ENV, BrowserAttachError
from .inspection import _sanitize_target_url
from .models import BrowserSelectorCount, BrowserSelectorInspection
from .playwright_snapshot import (
    _bounded_text,
    _load_playwright_sync_api,
    _operation_timeout_ms,
    _target_info,
)
from .selector_counts import (
    _MAX_MATCHES_PER_SELECTOR,
    _SELECTORS_ENV,
    BrowserSelectorCountError,
    _normalize_selectors,
)
from .snapshot import _SNAPSHOT_TARGET_ENV


def _run_count(
    endpoint: str,
    target_id: str,
    selectors: tuple[str, ...],
    timeout_seconds: float,
) -> BrowserSelectorInspection:
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
        try:
            for context in browser.contexts:
                for page in context.pages:
                    session = context.new_cdp_session(page)
                    keep = False
                    try:
                        info = _target_info(session.send("Target.getTargetInfo"))
                        if info["targetId"] == target_id:
                            matched_session = session
                            keep = True
                            break
                    finally:
                        if not keep:
                            session.detach()
                if matched_session is not None:
                    break
            if matched_session is None:
                raise BrowserSelectorCountError(
                    "requested browser target is not an attached page target"
                )

            target_info = _target_info(matched_session.send("Target.getTargetInfo"))
            if target_info["targetId"] != target_id or target_info["type"] != "page":
                raise BrowserSelectorCountError("requested browser target is not a page target")

            root_id = _document_root(
                matched_session.send(
                    "DOM.getDocument",
                    {"depth": 0, "pierce": False},
                )
            )
            counts = tuple(
                _count_selector(matched_session, root_id, selector, index)
                for index, selector in enumerate(selectors)
            )
            target_url = _sanitize_target_url(str(target_info.get("url") or ""))
            if not target_url:
                raise BrowserSelectorCountError("CDP page target has an invalid URL")
            return BrowserSelectorInspection(
                endpoint=endpoint,
                target_id=target_id,
                target_type="page",
                title=_bounded_text(target_info.get("title"), 512),
                url=target_url,
                selectors=counts,
            )
        finally:
            try:
                if matched_session is not None:
                    matched_session.detach()
            finally:
                browser.close(reason="host-ops read-only selector count complete")


def _document_root(response: object) -> int:
    if not isinstance(response, dict) or not isinstance(response.get("root"), dict):
        raise BrowserSelectorCountError("CDP DOM.getDocument returned invalid root data")
    root = cast(dict[str, Any], response["root"])
    node_id = root.get("nodeId")
    if not isinstance(node_id, int) or isinstance(node_id, bool) or node_id <= 0:
        raise BrowserSelectorCountError("CDP document root has an invalid node id")
    return node_id


def _count_selector(session: Any, root_id: int, selector: str, index: int) -> BrowserSelectorCount:
    try:
        response = session.send(
            "DOM.querySelectorAll",
            {"nodeId": root_id, "selector": selector},
        )
    except Exception as exc:
        raise BrowserSelectorCountError(f"selector query failed at index {index}") from exc
    if not isinstance(response, dict) or not isinstance(response.get("nodeIds"), list):
        raise BrowserSelectorCountError("CDP DOM.querySelectorAll returned invalid data")
    node_ids = response["nodeIds"]
    if len(node_ids) > _MAX_MATCHES_PER_SELECTOR:
        raise BrowserSelectorCountError("selector match count exceeded its bound")
    return BrowserSelectorCount(selector=selector, match_count=len(node_ids))


def _load_selectors(raw: str) -> tuple[str, ...]:
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise BrowserSelectorCountError("browser selector list is invalid JSON") from exc
    if not isinstance(value, list):
        raise BrowserSelectorCountError("browser selector list must be a JSON array")
    return _normalize_selectors(value)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--timeout", type=float, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    endpoint = os.environ.get(_ATTACH_ENDPOINT_ENV, "").strip()
    target_id = os.environ.get(_SNAPSHOT_TARGET_ENV, "").strip()
    selectors_raw = os.environ.get(_SELECTORS_ENV, "")
    if not endpoint or not target_id or not selectors_raw:
        print(json.dumps({"error": "browser selector-count input is missing"}, sort_keys=True))
        return 2
    if not 1 <= args.timeout <= 120:
        print(json.dumps({"error": "browser selector-count timeout is invalid"}, sort_keys=True))
        return 2
    try:
        selectors = _load_selectors(selectors_raw)
        result = _run_count(endpoint, target_id, selectors, args.timeout)
    except (BrowserSelectorCountError, BrowserAttachError, ValueError) as exc:
        print(json.dumps({"error": str(exc)[:1000]}, sort_keys=True))
        return 1
    except Exception:
        print(json.dumps({"error": "browser selector count failed"}, sort_keys=True))
        return 1
    print(json.dumps(result.as_dict(), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
