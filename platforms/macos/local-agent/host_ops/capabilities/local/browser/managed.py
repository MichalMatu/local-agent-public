"""Ephemeral managed-browser probing through a bounded Playwright helper process."""

from __future__ import annotations

import json
import re
import sys
from collections.abc import Callable
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from local_agent.host_ops.core.execution import ExecutionLimits, ProcessRunner, ProcessState

from ._validation import validate_timeout_seconds
from .models import ManagedBrowserProbe

SUPPORTED_BROWSER_ENGINES = ("chromium", "firefox", "webkit")
_MAX_URL_CHARS = 4096
_MAX_TITLE_CHARS = 512
_MAX_HELPER_OUTPUT_BYTES = 16 * 1024
_URL_IN_ERROR_RE = re.compile(r"https?://[^\s\"'<>]+")
_PROBE_URL_ENV = "HOST_OPS_MANAGED_BROWSER_URL"


class ManagedBrowserError(RuntimeError):
    """Raised when a managed browser probe cannot produce trustworthy evidence."""


ManagedProbeBackend = Callable[[str, str, float], ManagedBrowserProbe]


class ManagedBrowserProber:
    """Run one isolated managed-browser navigation and return bounded metadata."""

    def __init__(self, backend: ManagedProbeBackend | None = None) -> None:
        self._backend = backend or _probe_with_bounded_helper

    def probe(
        self,
        url: str,
        *,
        engine: str = "chromium",
        timeout_seconds: float = 15.0,
    ) -> ManagedBrowserProbe:
        target = _validate_navigation_url(url)
        normalized_engine = engine.strip().lower()
        if normalized_engine not in SUPPORTED_BROWSER_ENGINES:
            allowed = ", ".join(SUPPORTED_BROWSER_ENGINES)
            raise ValueError(f"browser engine must be one of: {allowed}")
        timeout_seconds = validate_timeout_seconds(timeout_seconds)
        return self._backend(target, normalized_engine, timeout_seconds)


def _probe_with_bounded_helper(
    url: str,
    engine: str,
    timeout_seconds: float,
) -> ManagedBrowserProbe:
    result = ProcessRunner().run(
        [
            sys.executable,
            "-m",
            "local_agent.host_ops.capabilities.local.browser.playwright_probe",
            "--engine",
            engine,
            "--timeout",
            f"{timeout_seconds:g}",
        ],
        env_overrides={_PROBE_URL_ENV: url},
        limits=ExecutionLimits(
            timeout_seconds=timeout_seconds,
            max_stdout_bytes=_MAX_HELPER_OUTPUT_BYTES,
            max_stderr_bytes=_MAX_HELPER_OUTPUT_BYTES,
        ),
    )
    if result.state is ProcessState.TIMED_OUT:
        raise ManagedBrowserError(
            f"managed browser probe exceeded {timeout_seconds:g}s whole-process timeout"
        )
    if result.stdout_truncated or result.stderr_truncated:
        raise ManagedBrowserError("managed browser probe helper output exceeded its bound")

    payload = _parse_helper_json(result.stdout)
    if not result.ok:
        detail = payload.get("error") if isinstance(payload.get("error"), str) else None
        if detail:
            raise ManagedBrowserError(_redact_probe_error(detail, url))
        raise ManagedBrowserError(
            f"managed browser probe helper failed: {result.state.value} exit={result.exit_code}"
        )
    return _probe_from_payload(payload, expected_engine=engine)


def _parse_helper_json(raw: str) -> dict[str, Any]:
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ManagedBrowserError("managed browser probe helper returned invalid JSON") from exc
    if not isinstance(payload, dict):
        raise ManagedBrowserError("managed browser probe helper returned a non-object JSON value")
    return payload


def _require_nonnegative_int(payload: dict[str, Any], key: str) -> int:
    value = payload.get(key)
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ManagedBrowserError("managed browser probe helper returned invalid event counts")
    return value


def _probe_from_payload(payload: dict[str, Any], *, expected_engine: str) -> ManagedBrowserProbe:
    engine = payload.get("engine")
    requested_url = payload.get("requested_url")
    final_url = payload.get("final_url")
    title = payload.get("title")
    status_code = payload.get("status_code")
    if engine != expected_engine:
        raise ManagedBrowserError("managed browser probe helper returned the wrong engine")
    if not isinstance(requested_url, str) or not requested_url:
        raise ManagedBrowserError("managed browser probe helper returned an invalid requested URL")
    if not isinstance(final_url, str) or not final_url:
        raise ManagedBrowserError("managed browser probe helper returned an invalid final URL")
    if not isinstance(title, str) or len(title) > _MAX_TITLE_CHARS:
        raise ManagedBrowserError("managed browser probe helper returned an invalid title")
    if status_code is not None and (
        not isinstance(status_code, int) or isinstance(status_code, bool)
    ):
        raise ManagedBrowserError("managed browser probe helper returned an invalid status code")
    console_errors = _require_nonnegative_int(payload, "console_error_count")
    page_errors = _require_nonnegative_int(payload, "page_error_count")
    request_failures = _require_nonnegative_int(payload, "request_failure_count")
    if _sanitize_public_url(requested_url) != requested_url:
        raise ManagedBrowserError(
            "managed browser probe helper returned an unsanitized requested URL"
        )
    if _sanitize_public_url(final_url) != final_url:
        raise ManagedBrowserError("managed browser probe helper returned an unsanitized final URL")

    return ManagedBrowserProbe(
        engine=engine,
        requested_url=requested_url,
        final_url=final_url,
        title=title,
        status_code=status_code,
        console_error_count=console_errors,
        page_error_count=page_errors,
        request_failure_count=request_failures,
    )


def _validate_navigation_url(raw: str) -> str:
    value = raw.strip()
    if not value or len(value) > _MAX_URL_CHARS:
        raise ValueError(f"browser URL must contain 1 to {_MAX_URL_CHARS} characters")
    if any(character.isspace() for character in value):
        raise ValueError("browser URL must not contain whitespace")
    try:
        parsed = urlsplit(value)
    except ValueError as exc:
        raise ValueError("browser URL is invalid") from exc
    if parsed.scheme not in {"http", "https"}:
        raise ValueError("browser URL must use http or https")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("browser URL must not contain credentials")
    if not parsed.hostname:
        raise ValueError("browser URL must include a hostname")
    return value


def _sanitize_public_url(value: str) -> str:
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        return ""
    host = parsed.hostname
    if parsed.scheme not in {"http", "https"} or not host:
        return ""
    authority_host = f"[{host}]" if ":" in host else host
    authority = authority_host if port is None else f"{authority_host}:{port}"
    return urlunsplit((parsed.scheme, authority, parsed.path, "", ""))


def _bounded_title(value: object) -> str:
    text = value if isinstance(value, str) else str(value or "")
    return text[:_MAX_TITLE_CHARS]


def _redact_probe_error(detail: str, target_url: str) -> str:
    def sanitize_match(match: re.Match[str]) -> str:
        sanitized = _sanitize_public_url(match.group(0))
        return sanitized or "<redacted-url>"

    redacted = _URL_IN_ERROR_RE.sub(sanitize_match, detail)
    sanitized_target = _sanitize_public_url(target_url)
    if target_url and sanitized_target:
        redacted = redacted.replace(target_url, sanitized_target)
    return redacted[:1000]
