"""Guarded reload of one exact live Chromium page target."""

from __future__ import annotations

import json
import sys
from collections.abc import Callable
from typing import Any
from urllib.parse import urlsplit

from local_agent.host_ops.core.execution import ExecutionLimits, ProcessRunner, ProcessState

from ._validation import validate_timeout_seconds
from .attach import _redact_attach_error
from .inspection import _normalize_loopback_endpoint, _sanitize_target_url
from .models import BrowserReloadResult
from .snapshot import _validate_target_id

_MAX_HELPER_OUTPUT_BYTES = 32 * 1024
_MAX_TITLE_CHARS = 512
_RELOAD_EXPECTED_URL_ENV = "HOST_OPS_BROWSER_RELOAD_EXPECTED_URL"
_EXPECTED_PAYLOAD_KEYS = frozenset(
    {
        "endpoint",
        "target_id",
        "target_type",
        "expected_url",
        "before_url",
        "before_title",
        "after_url",
        "after_title",
    }
)


class BrowserReloadError(RuntimeError):
    """Raised when a guarded browser reload cannot be completed safely."""


ReloadBackend = Callable[[str, str, str, float], BrowserReloadResult]


class BrowserCdpReloader:
    """Reload one exact HTTP(S) page target after a sanitized URL guard."""

    def __init__(self, backend: ReloadBackend | None = None) -> None:
        self._backend = backend or _reload_with_bounded_helper

    def reload(
        self,
        endpoint: str,
        target_id: str,
        expected_url: str,
        *,
        timeout_seconds: float = 15.0,
    ) -> BrowserReloadResult:
        normalized_endpoint = _normalize_loopback_endpoint(endpoint)
        normalized_target_id = _validate_target_id(target_id)
        normalized_expected_url = _normalize_expected_url(expected_url)
        timeout_seconds = validate_timeout_seconds(timeout_seconds)
        return self._backend(
            normalized_endpoint,
            normalized_target_id,
            normalized_expected_url,
            timeout_seconds,
        )


def _normalize_expected_url(raw: str) -> str:
    value = raw.strip()
    try:
        parsed = urlsplit(value)
        _ = parsed.port
    except ValueError as exc:
        raise ValueError("expected_url must be a valid sanitized HTTP(S) URL") from exc
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("expected_url must use HTTP(S) and include a host")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("expected_url must not contain credentials")
    if parsed.query or parsed.fragment:
        raise ValueError("expected_url must not contain a query or fragment")
    if _sanitize_target_url(value) != value:
        raise ValueError("expected_url must already be in sanitized browser-evidence form")
    return value


def _reload_with_bounded_helper(
    endpoint: str,
    target_id: str,
    expected_url: str,
    timeout_seconds: float,
) -> BrowserReloadResult:
    result = ProcessRunner().run(
        [
            sys.executable,
            "-m",
            "local_agent.host_ops.capabilities.local.browser.playwright_reload",
            "--timeout",
            f"{timeout_seconds:g}",
        ],
        env_overrides={
            "HOST_OPS_BROWSER_ATTACH_ENDPOINT": endpoint,
            "HOST_OPS_BROWSER_ATTACH_TARGET_ID": target_id,
            _RELOAD_EXPECTED_URL_ENV: expected_url,
        },
        limits=ExecutionLimits(
            timeout_seconds=timeout_seconds,
            max_stdout_bytes=_MAX_HELPER_OUTPUT_BYTES,
            max_stderr_bytes=_MAX_HELPER_OUTPUT_BYTES,
        ),
    )
    if result.state is ProcessState.TIMED_OUT:
        raise BrowserReloadError(
            f"browser reload exceeded {timeout_seconds:g}s whole-process timeout"
        )
    if result.stdout_truncated or result.stderr_truncated:
        raise BrowserReloadError("browser reload helper output exceeded its bound")

    payload = _parse_helper_json(result.stdout)
    if not result.ok:
        detail = payload.get("error") if isinstance(payload.get("error"), str) else None
        if detail:
            raise BrowserReloadError(_redact_attach_error(detail, endpoint))
        raise BrowserReloadError(
            f"browser reload helper failed: {result.state.value} exit={result.exit_code}"
        )
    return _result_from_payload(
        payload,
        endpoint=endpoint,
        target_id=target_id,
        expected_url=expected_url,
    )


def _parse_helper_json(raw: str) -> dict[str, Any]:
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise BrowserReloadError("browser reload helper returned invalid JSON") from exc
    if not isinstance(payload, dict):
        raise BrowserReloadError("browser reload helper returned a non-object JSON value")
    return payload


def _validated_url(payload: dict[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value or _sanitize_target_url(value) != value:
        raise BrowserReloadError(f"browser reload helper returned invalid {key}")
    try:
        parsed = urlsplit(value)
        _ = parsed.port
    except ValueError as exc:
        raise BrowserReloadError(f"browser reload helper returned invalid {key}") from exc
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise BrowserReloadError(f"browser reload helper returned invalid {key}")
    return value


def _validated_title(payload: dict[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or len(value) > _MAX_TITLE_CHARS:
        raise BrowserReloadError(f"browser reload helper returned invalid {key}")
    return value


def _result_from_payload(
    payload: dict[str, Any], *, endpoint: str, target_id: str, expected_url: str
) -> BrowserReloadResult:
    if set(payload) != _EXPECTED_PAYLOAD_KEYS:
        raise BrowserReloadError("browser reload helper returned an unexpected payload shape")
    if payload.get("endpoint") != endpoint:
        raise BrowserReloadError("browser reload helper returned wrong endpoint")
    if payload.get("target_id") != target_id:
        raise BrowserReloadError("browser reload helper returned wrong target id")
    if payload.get("target_type") != "page":
        raise BrowserReloadError("browser reload helper returned a non-page target")
    if payload.get("expected_url") != expected_url:
        raise BrowserReloadError("browser reload helper returned wrong expected URL")

    before_url = _validated_url(payload, "before_url")
    if before_url != expected_url:
        raise BrowserReloadError("browser reload helper violated the expected URL guard")
    after_url = _validated_url(payload, "after_url")
    before_title = _validated_title(payload, "before_title")
    after_title = _validated_title(payload, "after_title")

    return BrowserReloadResult(
        endpoint=endpoint,
        target_id=target_id,
        target_type="page",
        expected_url=expected_url,
        before_url=before_url,
        before_title=before_title,
        after_url=after_url,
        after_title=after_title,
    )
