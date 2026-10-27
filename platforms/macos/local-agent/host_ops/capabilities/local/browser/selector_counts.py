"""Bounded CSS selector match counts for one exact live Chromium page target."""

from __future__ import annotations

import json
import sys
from collections.abc import Callable, Sequence
from typing import Any

from local_agent.host_ops.core.execution import ExecutionLimits, ProcessRunner, ProcessState

from ._validation import validate_timeout_seconds
from .attach import _redact_attach_error
from .inspection import _normalize_loopback_endpoint, _sanitize_target_url
from .models import BrowserSelectorCount, BrowserSelectorInspection
from .snapshot import _validate_target_id

_MAX_HELPER_OUTPUT_BYTES = 32 * 1024
_MAX_SELECTORS = 16
_MAX_SELECTOR_CHARS = 256
_MAX_MATCHES_PER_SELECTOR = 100_000
_SELECTORS_ENV = "HOST_OPS_BROWSER_SELECTOR_QUERIES"


class BrowserSelectorCountError(RuntimeError):
    """Raised when selector-count evidence cannot be trusted."""


SelectorBackend = Callable[[str, str, tuple[str, ...], float], BrowserSelectorInspection]


class BrowserCdpSelectorCounter:
    """Count bounded CSS selector matches without returning page content or node ids."""

    def __init__(self, backend: SelectorBackend | None = None) -> None:
        self._backend = backend or _count_with_bounded_helper

    def count(
        self,
        endpoint: str,
        target_id: str,
        selectors: Sequence[str],
        *,
        timeout_seconds: float = 10.0,
    ) -> BrowserSelectorInspection:
        normalized_endpoint = _normalize_loopback_endpoint(endpoint)
        normalized_target_id = _validate_target_id(target_id)
        normalized_selectors = _normalize_selectors(selectors)
        timeout_seconds = validate_timeout_seconds(timeout_seconds)
        return self._backend(
            normalized_endpoint, normalized_target_id, normalized_selectors, timeout_seconds
        )


def _normalize_selectors(selectors: Sequence[str]) -> tuple[str, ...]:
    raw = tuple(selectors)
    if not 1 <= len(raw) <= _MAX_SELECTORS:
        raise ValueError("selectors must contain between 1 and 16 CSS selectors")
    normalized: list[str] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, str):
            raise ValueError("each selector must be a string")
        value = item.strip()
        if not value or len(value) > _MAX_SELECTOR_CHARS:
            raise ValueError("each selector must contain 1 to 256 characters")
        if any(ord(character) < 32 or ord(character) == 127 for character in value):
            raise ValueError("selectors must not contain control characters")
        if value not in seen:
            seen.add(value)
            normalized.append(value)
    return tuple(normalized)


def _count_with_bounded_helper(
    endpoint: str, target_id: str, selectors: tuple[str, ...], timeout_seconds: float
) -> BrowserSelectorInspection:
    result = ProcessRunner().run(
        [
            sys.executable,
            "-m",
            "local_agent.host_ops.capabilities.local.browser.playwright_selector_counts",
            "--timeout",
            f"{timeout_seconds:g}",
        ],
        env_overrides={
            "HOST_OPS_BROWSER_ATTACH_ENDPOINT": endpoint,
            "HOST_OPS_BROWSER_ATTACH_TARGET_ID": target_id,
            _SELECTORS_ENV: json.dumps(selectors),
        },
        limits=ExecutionLimits(
            timeout_seconds=timeout_seconds,
            max_stdout_bytes=_MAX_HELPER_OUTPUT_BYTES,
            max_stderr_bytes=_MAX_HELPER_OUTPUT_BYTES,
        ),
    )
    if result.state is ProcessState.TIMED_OUT:
        raise BrowserSelectorCountError(
            f"browser selector count exceeded {timeout_seconds:g}s whole-process timeout"
        )
    if result.stdout_truncated or result.stderr_truncated:
        raise BrowserSelectorCountError("browser selector-count helper output exceeded its bound")

    payload = _parse_helper_json(result.stdout)
    if not result.ok:
        detail = payload.get("error") if isinstance(payload.get("error"), str) else None
        if detail:
            raise BrowserSelectorCountError(_redact_attach_error(detail, endpoint))
        raise BrowserSelectorCountError(
            f"browser selector-count helper failed: {result.state.value} exit={result.exit_code}"
        )
    return _inspection_from_payload(
        payload, endpoint=endpoint, target_id=target_id, selectors=selectors
    )


def _parse_helper_json(raw: str) -> dict[str, Any]:
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise BrowserSelectorCountError(
            "browser selector-count helper returned invalid JSON"
        ) from exc
    if not isinstance(payload, dict):
        raise BrowserSelectorCountError(
            "browser selector-count helper returned a non-object JSON value"
        )
    return payload


def _inspection_from_payload(
    payload: dict[str, Any], *, endpoint: str, target_id: str, selectors: tuple[str, ...]
) -> BrowserSelectorInspection:
    if payload.get("endpoint") != endpoint:
        raise BrowserSelectorCountError("browser selector-count helper returned wrong endpoint")
    if payload.get("target_id") != target_id:
        raise BrowserSelectorCountError("browser selector-count helper returned wrong target id")
    if payload.get("target_type") != "page":
        raise BrowserSelectorCountError("browser selector-count helper returned a non-page target")
    title = payload.get("title")
    url = payload.get("url")
    if not isinstance(title, str) or len(title) > 512:
        raise BrowserSelectorCountError("browser selector-count helper returned invalid title")
    if not isinstance(url, str) or _sanitize_target_url(url) != url:
        raise BrowserSelectorCountError("browser selector-count helper returned unsanitized url")
    raw_counts = payload.get("selectors")
    if not isinstance(raw_counts, list) or len(raw_counts) != len(selectors):
        raise BrowserSelectorCountError(
            "browser selector-count helper returned invalid selector list"
        )
    counts: list[BrowserSelectorCount] = []
    for expected, item in zip(selectors, raw_counts, strict=True):
        if not isinstance(item, dict) or item.get("selector") != expected:
            raise BrowserSelectorCountError("browser selector-count helper returned wrong selector")
        match_count = item.get("match_count")
        if (
            not isinstance(match_count, int)
            or isinstance(match_count, bool)
            or not 0 <= match_count <= _MAX_MATCHES_PER_SELECTOR
        ):
            raise BrowserSelectorCountError(
                "browser selector-count helper returned invalid match count"
            )
        counts.append(BrowserSelectorCount(selector=expected, match_count=match_count))
    return BrowserSelectorInspection(
        endpoint=endpoint,
        target_id=target_id,
        target_type="page",
        title=title,
        url=url,
        selectors=tuple(counts),
    )
