"""Read-only Chromium CDP attachment through a bounded Playwright helper."""

from __future__ import annotations

import json
import re
import sys
from collections.abc import Callable
from typing import Any

from local_agent.host_ops.core.execution import ExecutionLimits, ProcessRunner, ProcessState

from ._validation import validate_timeout_seconds
from .inspection import _normalize_loopback_endpoint, _sanitize_target_url
from .models import BrowserAttachInspection, BrowserAttachTarget

_MAX_HELPER_OUTPUT_BYTES = 64 * 1024
_MAX_TARGETS = 512
_MAX_TARGET_ID_CHARS = 256
_MAX_TARGET_TYPE_CHARS = 64
_MAX_TITLE_CHARS = 512
_MAX_BROWSER_VERSION_CHARS = 128
_ATTACH_ENDPOINT_ENV = "HOST_OPS_BROWSER_ATTACH_ENDPOINT"
_URL_IN_ERROR_RE = re.compile(r"(?:https?|wss?)://[^\s\"'<>]+")


class BrowserAttachError(RuntimeError):
    """Raised when a read-only CDP attachment cannot produce trustworthy evidence."""


AttachBackend = Callable[[str, float], BrowserAttachInspection]


class BrowserCdpAttacher:
    """Attach read-only to one explicit local Chromium CDP endpoint."""

    def __init__(self, backend: AttachBackend | None = None) -> None:
        self._backend = backend or _inspect_with_bounded_helper

    def inspect(
        self,
        endpoint: str,
        *,
        timeout_seconds: float = 10.0,
    ) -> BrowserAttachInspection:
        normalized_endpoint = _normalize_loopback_endpoint(endpoint)
        timeout_seconds = validate_timeout_seconds(timeout_seconds)
        return self._backend(normalized_endpoint, timeout_seconds)


def _inspect_with_bounded_helper(endpoint: str, timeout_seconds: float) -> BrowserAttachInspection:
    result = ProcessRunner().run(
        [
            sys.executable,
            "-m",
            "local_agent.host_ops.capabilities.local.browser.playwright_attach",
            "--timeout",
            f"{timeout_seconds:g}",
        ],
        env_overrides={_ATTACH_ENDPOINT_ENV: endpoint},
        limits=ExecutionLimits(
            timeout_seconds=timeout_seconds,
            max_stdout_bytes=_MAX_HELPER_OUTPUT_BYTES,
            max_stderr_bytes=_MAX_HELPER_OUTPUT_BYTES,
        ),
    )
    if result.state is ProcessState.TIMED_OUT:
        raise BrowserAttachError(
            f"browser CDP attach exceeded {timeout_seconds:g}s whole-process timeout"
        )
    if result.stdout_truncated or result.stderr_truncated:
        raise BrowserAttachError("browser CDP attach helper output exceeded its bound")

    payload = _parse_helper_json(result.stdout)
    if not result.ok:
        detail = payload.get("error") if isinstance(payload.get("error"), str) else None
        if detail:
            raise BrowserAttachError(_redact_attach_error(detail, endpoint))
        raise BrowserAttachError(
            f"browser CDP attach helper failed: {result.state.value} exit={result.exit_code}"
        )
    return _inspection_from_payload(payload, expected_endpoint=endpoint)


def _parse_helper_json(raw: str) -> dict[str, Any]:
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise BrowserAttachError("browser CDP attach helper returned invalid JSON") from exc
    if not isinstance(payload, dict):
        raise BrowserAttachError("browser CDP attach helper returned a non-object JSON value")
    return payload


def _inspection_from_payload(
    payload: dict[str, Any],
    *,
    expected_endpoint: str,
) -> BrowserAttachInspection:
    endpoint = payload.get("endpoint")
    browser_version = payload.get("browser_version")
    context_count = payload.get("context_count")
    targets_raw = payload.get("targets")

    if endpoint != expected_endpoint:
        raise BrowserAttachError("browser CDP attach helper returned the wrong endpoint")
    if not isinstance(browser_version, str) or not browser_version:
        raise BrowserAttachError("browser CDP attach helper returned an invalid browser version")
    if len(browser_version) > _MAX_BROWSER_VERSION_CHARS:
        raise BrowserAttachError("browser CDP attach helper returned an oversized browser version")
    if not isinstance(context_count, int) or isinstance(context_count, bool) or context_count < 0:
        raise BrowserAttachError("browser CDP attach helper returned an invalid context count")
    if not isinstance(targets_raw, list) or len(targets_raw) > _MAX_TARGETS:
        raise BrowserAttachError("browser CDP attach helper returned an invalid target list")

    targets = tuple(_target_from_payload(item) for item in targets_raw)
    return BrowserAttachInspection(
        endpoint=endpoint,
        browser_version=browser_version,
        context_count=context_count,
        targets=targets,
    )


def _target_from_payload(raw: object) -> BrowserAttachTarget:
    if not isinstance(raw, dict):
        raise BrowserAttachError("browser CDP attach helper returned an invalid target")
    target_id = raw.get("id")
    target_type = raw.get("type")
    title = raw.get("title")
    url = raw.get("url")
    attached = raw.get("attached")
    if not isinstance(target_id, str) or not 1 <= len(target_id) <= _MAX_TARGET_ID_CHARS:
        raise BrowserAttachError("browser CDP attach helper returned an invalid target id")
    if not isinstance(target_type, str) or not 1 <= len(target_type) <= _MAX_TARGET_TYPE_CHARS:
        raise BrowserAttachError("browser CDP attach helper returned an invalid target type")
    if not isinstance(title, str) or len(title) > _MAX_TITLE_CHARS:
        raise BrowserAttachError("browser CDP attach helper returned an invalid target title")
    if not isinstance(url, str) or _sanitize_target_url(url) != url:
        raise BrowserAttachError("browser CDP attach helper returned an unsanitized target URL")
    if not isinstance(attached, bool):
        raise BrowserAttachError("browser CDP attach helper returned an invalid attached state")
    return BrowserAttachTarget(
        target_id=target_id,
        target_type=target_type,
        title=title,
        url=url,
        attached=attached,
    )


def _redact_attach_error(detail: str, endpoint: str) -> str:
    def sanitize_match(match: re.Match[str]) -> str:
        value = match.group(0)
        if value.startswith(("ws://", "wss://")):
            return "<redacted-cdp-url>"
        try:
            return _normalize_loopback_endpoint(value)
        except ValueError:
            return "<redacted-url>"

    redacted = _URL_IN_ERROR_RE.sub(sanitize_match, detail)
    redacted = redacted.replace(endpoint, endpoint)
    return redacted[:1000]
