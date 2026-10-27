"""Correlate page DOM readiness with exact extension script fingerprints."""

from __future__ import annotations

import json
import re
import sys
from collections.abc import Callable, Sequence
from typing import Any

from local_agent.host_ops.core.execution import ExecutionLimits, ProcessRunner, ProcessState

from ._validation import validate_timeout_seconds
from .attach import _redact_attach_error
from .inspection import _normalize_loopback_endpoint, _sanitize_target_url
from .models import (
    BrowserExtensionScriptEvidence,
    BrowserReadinessInspection,
    BrowserSelectorCount,
)
from .selector_counts import _MAX_MATCHES_PER_SELECTOR, _normalize_selectors
from .snapshot import _validate_target_id

_MAX_HELPER_OUTPUT_BYTES = 32 * 1024
_MAX_EXTENSION_SCRIPTS = 16
_MAX_SCRIPT_NAME_CHARS = 128
_SCRIPT_NAME_RE = re.compile(r"^[A-Za-z0-9._-]+$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_SCRIPT_FINGERPRINTS_ENV = "HOST_OPS_BROWSER_EXTENSION_SCRIPT_FINGERPRINTS"
_CONTENT_STATES = frozenset({"missing", "stale", "ready", "ambiguous"})
_WORKER_STATES = frozenset({"unknown", "inactive", "running"})
_DIAGNOSES = frozenset(
    {
        "dom_not_ready",
        "content_script_missing",
        "content_script_stale",
        "extension_ambiguous",
        "worker_inactive",
        "ready",
    }
)
_SCRIPT_STATUSES = frozenset({"missing", "stale", "matched"})


class BrowserReadinessError(RuntimeError):
    """Raised when combined DOM/extension readiness evidence cannot be trusted."""


ScriptFingerprint = tuple[str, str]
ReadinessBackend = Callable[
    [str, str, tuple[str, ...], tuple[ScriptFingerprint, ...], float],
    BrowserReadinessInspection,
]


class BrowserCdpReadinessInspector:
    """Read bounded DOM and extension-script readiness for one exact page target."""

    def __init__(self, backend: ReadinessBackend | None = None) -> None:
        self._backend = backend or _inspect_with_bounded_helper

    def inspect(
        self,
        endpoint: str,
        target_id: str,
        selectors: Sequence[str],
        script_fingerprints: Sequence[str],
        *,
        timeout_seconds: float = 10.0,
    ) -> BrowserReadinessInspection:
        normalized_endpoint = _normalize_loopback_endpoint(endpoint)
        normalized_target_id = _validate_target_id(target_id)
        normalized_selectors = _normalize_selectors(selectors)
        normalized_fingerprints = _normalize_script_fingerprints(script_fingerprints)
        timeout_seconds = validate_timeout_seconds(timeout_seconds)
        return self._backend(
            normalized_endpoint,
            normalized_target_id,
            normalized_selectors,
            normalized_fingerprints,
            timeout_seconds,
        )


def _normalize_script_fingerprints(values: Sequence[str]) -> tuple[ScriptFingerprint, ...]:
    raw = tuple(values)
    if not 1 <= len(raw) <= _MAX_EXTENSION_SCRIPTS:
        raise ValueError("script_fingerprints must contain between 1 and 16 entries")
    normalized: list[ScriptFingerprint] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, str) or "=" not in item:
            raise ValueError("each script fingerprint must use NAME=SHA256")
        name, digest = item.split("=", 1)
        name = name.strip()
        digest = digest.strip().lower()
        if (
            not name
            or len(name) > _MAX_SCRIPT_NAME_CHARS
            or not _SCRIPT_NAME_RE.fullmatch(name)
            or "/" in name
            or "\\" in name
        ):
            raise ValueError("script fingerprint names must be safe basenames up to 128 characters")
        if not _SHA256_RE.fullmatch(digest):
            raise ValueError("script fingerprint SHA256 values must be 64 lowercase hex characters")
        if name in seen:
            raise ValueError("script fingerprint names must be unique")
        seen.add(name)
        normalized.append((name, digest))
    return tuple(normalized)


def _inspect_with_bounded_helper(
    endpoint: str,
    target_id: str,
    selectors: tuple[str, ...],
    script_fingerprints: tuple[ScriptFingerprint, ...],
    timeout_seconds: float,
) -> BrowserReadinessInspection:
    result = ProcessRunner().run(
        [
            sys.executable,
            "-m",
            "local_agent.host_ops.capabilities.local.browser.playwright_readiness",
            "--timeout",
            f"{timeout_seconds:g}",
        ],
        env_overrides={
            "HOST_OPS_BROWSER_ATTACH_ENDPOINT": endpoint,
            "HOST_OPS_BROWSER_ATTACH_TARGET_ID": target_id,
            "HOST_OPS_BROWSER_SELECTOR_QUERIES": json.dumps(selectors),
            _SCRIPT_FINGERPRINTS_ENV: json.dumps(script_fingerprints),
        },
        limits=ExecutionLimits(
            timeout_seconds=timeout_seconds,
            max_stdout_bytes=_MAX_HELPER_OUTPUT_BYTES,
            max_stderr_bytes=_MAX_HELPER_OUTPUT_BYTES,
        ),
    )
    if result.state is ProcessState.TIMED_OUT:
        raise BrowserReadinessError(
            f"browser readiness inspection exceeded {timeout_seconds:g}s whole-process timeout"
        )
    if result.stdout_truncated or result.stderr_truncated:
        raise BrowserReadinessError("browser readiness helper output exceeded its bound")
    payload = _parse_helper_json(result.stdout)
    if not result.ok:
        detail = payload.get("error") if isinstance(payload.get("error"), str) else None
        if detail:
            raise BrowserReadinessError(_redact_attach_error(detail, endpoint))
        raise BrowserReadinessError(
            f"browser readiness helper failed: {result.state.value} exit={result.exit_code}"
        )
    return _inspection_from_payload(
        payload,
        endpoint=endpoint,
        target_id=target_id,
        selectors=selectors,
        script_fingerprints=script_fingerprints,
    )


def _parse_helper_json(raw: str) -> dict[str, Any]:
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise BrowserReadinessError("browser readiness helper returned invalid JSON") from exc
    if not isinstance(payload, dict):
        raise BrowserReadinessError("browser readiness helper returned a non-object JSON value")
    return payload


def _selector_evidence(raw: object, selectors: tuple[str, ...]) -> tuple[BrowserSelectorCount, ...]:
    if not isinstance(raw, list) or len(raw) != len(selectors):
        raise BrowserReadinessError("browser readiness helper returned invalid selector list")
    evidence: list[BrowserSelectorCount] = []
    for expected, item in zip(selectors, raw, strict=True):
        if not isinstance(item, dict) or item.get("selector") != expected:
            raise BrowserReadinessError("browser readiness helper returned wrong selector")
        count = item.get("match_count")
        if (
            not isinstance(count, int)
            or isinstance(count, bool)
            or not 0 <= count <= _MAX_MATCHES_PER_SELECTOR
        ):
            raise BrowserReadinessError("browser readiness helper returned invalid match count")
        evidence.append(BrowserSelectorCount(selector=expected, match_count=count))
    return tuple(evidence)


def _script_evidence(
    raw: object, script_fingerprints: tuple[ScriptFingerprint, ...]
) -> tuple[BrowserExtensionScriptEvidence, ...]:
    if not isinstance(raw, list) or len(raw) != len(script_fingerprints):
        raise BrowserReadinessError(
            "browser readiness helper returned invalid extension script list"
        )
    evidence: list[BrowserExtensionScriptEvidence] = []
    for (expected_name, _digest), item in zip(script_fingerprints, raw, strict=True):
        if not isinstance(item, dict) or item.get("name") != expected_name:
            raise BrowserReadinessError("browser readiness helper returned wrong extension script")
        status = item.get("status")
        if status not in _SCRIPT_STATUSES:
            raise BrowserReadinessError("browser readiness helper returned invalid script status")
        evidence.append(BrowserExtensionScriptEvidence(name=expected_name, status=status))
    return tuple(evidence)


def _inspection_from_payload(
    payload: dict[str, Any],
    *,
    endpoint: str,
    target_id: str,
    selectors: tuple[str, ...],
    script_fingerprints: tuple[ScriptFingerprint, ...],
) -> BrowserReadinessInspection:
    if payload.get("endpoint") != endpoint:
        raise BrowserReadinessError("browser readiness helper returned wrong endpoint")
    if payload.get("target_id") != target_id or payload.get("target_type") != "page":
        raise BrowserReadinessError("browser readiness helper returned wrong page target")
    title = payload.get("title")
    url = payload.get("url")
    if not isinstance(title, str) or len(title) > 512:
        raise BrowserReadinessError("browser readiness helper returned invalid title")
    if not isinstance(url, str) or _sanitize_target_url(url) != url:
        raise BrowserReadinessError("browser readiness helper returned unsanitized url")
    selector_evidence = _selector_evidence(payload.get("selectors"), selectors)
    script_evidence = _script_evidence(payload.get("extension_scripts"), script_fingerprints)
    dom_ready = payload.get("dom_ready")
    content_state = payload.get("content_script_state")
    worker_state = payload.get("worker_state")
    diagnosis = payload.get("diagnosis")
    if not isinstance(dom_ready, bool):
        raise BrowserReadinessError("browser readiness helper returned invalid DOM readiness")
    if content_state not in _CONTENT_STATES:
        raise BrowserReadinessError(
            "browser readiness helper returned invalid content-script state"
        )
    if worker_state not in _WORKER_STATES:
        raise BrowserReadinessError("browser readiness helper returned invalid worker state")
    if diagnosis not in _DIAGNOSES:
        raise BrowserReadinessError("browser readiness helper returned invalid diagnosis")
    if dom_ready != all(item.match_count > 0 for item in selector_evidence):
        raise BrowserReadinessError("browser readiness helper returned inconsistent DOM readiness")
    return BrowserReadinessInspection(
        endpoint=endpoint,
        target_id=target_id,
        target_type="page",
        title=title,
        url=url,
        selectors=selector_evidence,
        extension_scripts=script_evidence,
        dom_ready=dom_ready,
        content_script_state=content_state,
        worker_state=worker_state,
        diagnosis=diagnosis,
    )
