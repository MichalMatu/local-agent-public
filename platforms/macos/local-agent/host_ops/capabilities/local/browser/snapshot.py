"""Target-specific read-only page snapshots through a bounded Playwright CDP helper."""

from __future__ import annotations

import json
import sys
from collections.abc import Callable
from typing import Any

from local_agent.host_ops.core.execution import ExecutionLimits, ProcessRunner, ProcessState

from ._validation import validate_timeout_seconds
from .attach import _redact_attach_error
from .inspection import _normalize_loopback_endpoint, _sanitize_target_url
from .models import BrowserPageSnapshot

_MAX_HELPER_OUTPUT_BYTES = 32 * 1024
_MAX_TARGET_ID_CHARS = 256
_MAX_TITLE_CHARS = 512
_MAX_TARGET_TYPE_CHARS = 64
_MAX_MIME_TYPE_CHARS = 128
_MAX_FRAMES = 2048
_SNAPSHOT_TARGET_ENV = "HOST_OPS_BROWSER_ATTACH_TARGET_ID"


class BrowserSnapshotError(RuntimeError):
    """Raised when a target-specific browser snapshot is not trustworthy."""


SnapshotBackend = Callable[[str, str, float], BrowserPageSnapshot]


class BrowserCdpSnapshotter:
    """Read bounded metadata from one exact page target without page mutation."""

    def __init__(self, backend: SnapshotBackend | None = None) -> None:
        self._backend = backend or _snapshot_with_bounded_helper

    def snapshot(
        self,
        endpoint: str,
        target_id: str,
        *,
        timeout_seconds: float = 10.0,
    ) -> BrowserPageSnapshot:
        normalized_endpoint = _normalize_loopback_endpoint(endpoint)
        normalized_target_id = _validate_target_id(target_id)
        timeout_seconds = validate_timeout_seconds(timeout_seconds)
        return self._backend(normalized_endpoint, normalized_target_id, timeout_seconds)


def _validate_target_id(raw: str) -> str:
    value = raw.strip()
    if not value or len(value) > _MAX_TARGET_ID_CHARS:
        raise ValueError("browser target id must contain 1 to 256 characters")
    if any(character.isspace() or ord(character) < 32 for character in value):
        raise ValueError("browser target id must not contain whitespace or control characters")
    return value


def _snapshot_with_bounded_helper(
    endpoint: str, target_id: str, timeout_seconds: float
) -> BrowserPageSnapshot:
    result = ProcessRunner().run(
        [
            sys.executable,
            "-m",
            "local_agent.host_ops.capabilities.local.browser.playwright_snapshot",
            "--timeout",
            f"{timeout_seconds:g}",
        ],
        env_overrides={
            "HOST_OPS_BROWSER_ATTACH_ENDPOINT": endpoint,
            _SNAPSHOT_TARGET_ENV: target_id,
        },
        limits=ExecutionLimits(
            timeout_seconds=timeout_seconds,
            max_stdout_bytes=_MAX_HELPER_OUTPUT_BYTES,
            max_stderr_bytes=_MAX_HELPER_OUTPUT_BYTES,
        ),
    )
    if result.state is ProcessState.TIMED_OUT:
        raise BrowserSnapshotError(
            f"browser CDP snapshot exceeded {timeout_seconds:g}s whole-process timeout"
        )
    if result.stdout_truncated or result.stderr_truncated:
        raise BrowserSnapshotError("browser CDP snapshot helper output exceeded its bound")

    payload = _parse_helper_json(result.stdout)
    if not result.ok:
        detail = payload.get("error") if isinstance(payload.get("error"), str) else None
        if detail:
            raise BrowserSnapshotError(_redact_attach_error(detail, endpoint))
        raise BrowserSnapshotError(
            f"browser CDP snapshot helper failed: {result.state.value} exit={result.exit_code}"
        )
    return _snapshot_from_payload(payload, expected_endpoint=endpoint, expected_target_id=target_id)


def _parse_helper_json(raw: str) -> dict[str, Any]:
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise BrowserSnapshotError("browser CDP snapshot helper returned invalid JSON") from exc
    if not isinstance(payload, dict):
        raise BrowserSnapshotError("browser CDP snapshot helper returned a non-object JSON value")
    return payload


def _bounded_string(
    payload: dict[str, Any], key: str, maximum: int, *, allow_empty: bool = False
) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or len(value) > maximum or (not allow_empty and not value):
        raise BrowserSnapshotError(f"browser CDP snapshot helper returned invalid {key}")
    return value


def _optional_sanitized_url(payload: dict[str, Any], key: str) -> str | None:
    value = payload.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or _sanitize_target_url(value) != value:
        raise BrowserSnapshotError(f"browser CDP snapshot helper returned unsanitized {key}")
    return value


def _nonnegative_int(payload: dict[str, Any], key: str, maximum: int) -> int:
    value = payload.get(key)
    if not isinstance(value, int) or isinstance(value, bool) or value < 0 or value > maximum:
        raise BrowserSnapshotError(f"browser CDP snapshot helper returned invalid {key}")
    return value


def _snapshot_from_payload(
    payload: dict[str, Any], *, expected_endpoint: str, expected_target_id: str
) -> BrowserPageSnapshot:
    if payload.get("endpoint") != expected_endpoint:
        raise BrowserSnapshotError("browser CDP snapshot helper returned the wrong endpoint")
    if payload.get("target_id") != expected_target_id:
        raise BrowserSnapshotError("browser CDP snapshot helper returned the wrong target id")

    target_type = _bounded_string(payload, "target_type", _MAX_TARGET_TYPE_CHARS)
    if target_type != "page":
        raise BrowserSnapshotError("browser CDP snapshot helper returned a non-page target")
    title = _bounded_string(payload, "title", _MAX_TITLE_CHARS, allow_empty=True)
    url = _optional_sanitized_url(payload, "url")
    if url is None:
        raise BrowserSnapshotError("browser CDP snapshot helper returned an invalid url")
    frame_count = _nonnegative_int(payload, "frame_count", _MAX_FRAMES)
    main_frame_url = _optional_sanitized_url(payload, "main_frame_url")
    if main_frame_url is None:
        raise BrowserSnapshotError("browser CDP snapshot helper returned invalid main_frame_url")
    main_frame_mime_type = _bounded_string(
        payload, "main_frame_mime_type", _MAX_MIME_TYPE_CHARS, allow_empty=True
    )
    document_node_name = _bounded_string(payload, "document_node_name", 64, allow_empty=False)
    document_child_count = _nonnegative_int(payload, "document_child_count", 100000)

    return BrowserPageSnapshot(
        endpoint=expected_endpoint,
        target_id=expected_target_id,
        target_type=target_type,
        title=title,
        url=url,
        frame_count=frame_count,
        main_frame_url=main_frame_url,
        main_frame_mime_type=main_frame_mime_type,
        document_node_name=document_node_name,
        document_child_count=document_child_count,
    )
