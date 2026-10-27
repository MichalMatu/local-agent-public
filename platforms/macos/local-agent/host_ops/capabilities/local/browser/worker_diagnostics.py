"""Read-only worker diagnostics for one explicit Chromium CDP endpoint."""

from __future__ import annotations

import json
import sys
from collections.abc import Callable
from typing import Any

from local_agent.host_ops.core.execution import ExecutionLimits, ProcessRunner, ProcessState

from ._validation import validate_timeout_seconds
from .attach import _ATTACH_ENDPOINT_ENV, _redact_attach_error
from .inspection import _normalize_loopback_endpoint, _sanitize_target_url
from .models import (
    BrowserServiceWorkerRegistration,
    BrowserServiceWorkerVersion,
    BrowserWorkerDiagnostics,
    BrowserWorkerTarget,
)
from .snapshot import _validate_target_id

_MAX_HELPER_OUTPUT_BYTES = 64 * 1024
_MAX_WORKER_TARGETS = 512
_MAX_REGISTRATIONS = 512
_MAX_VERSIONS = 1024
_MAX_CONTROLLED_CLIENTS = 4096
_MAX_ERROR_COUNT = 100000
_MAX_TARGET_TYPE_CHARS = 64
_WORKER_TARGET_TYPES = frozenset({"service_worker", "worker", "shared_worker", "background_page"})
_RUNNING_STATUSES = frozenset({"stopped", "starting", "running", "stopping"})
_LIFECYCLE_STATUSES = frozenset(
    {"new", "installing", "installed", "activating", "activated", "redundant"}
)
_EXPECTED_PAYLOAD_KEYS = frozenset(
    {"endpoint", "worker_targets", "registrations", "versions", "error_count"}
)
_TARGET_KEYS = frozenset({"id", "type", "url", "attached"})
_REGISTRATION_KEYS = frozenset({"scope_url", "is_deleted"})
_VERSION_KEYS = frozenset(
    {
        "target_id",
        "scope_url",
        "script_url",
        "running_status",
        "lifecycle_status",
        "controlled_client_count",
        "target_attached",
        "registration_deleted",
    }
)


class BrowserWorkerDiagnosticsError(RuntimeError):
    """Raised when Chromium worker diagnostics cannot produce trustworthy evidence."""


WorkerDiagnosticsBackend = Callable[[str, float], BrowserWorkerDiagnostics]


class BrowserCdpWorkerDiagnoser:
    """Read bounded worker/service-worker state from one explicit Chromium endpoint."""

    def __init__(self, backend: WorkerDiagnosticsBackend | None = None) -> None:
        self._backend = backend or _inspect_with_bounded_helper

    def inspect(
        self,
        endpoint: str,
        *,
        timeout_seconds: float = 10.0,
    ) -> BrowserWorkerDiagnostics:
        normalized_endpoint = _normalize_loopback_endpoint(endpoint)
        timeout_seconds = validate_timeout_seconds(timeout_seconds)
        return self._backend(normalized_endpoint, timeout_seconds)


def _inspect_with_bounded_helper(endpoint: str, timeout_seconds: float) -> BrowserWorkerDiagnostics:
    result = ProcessRunner().run(
        [
            sys.executable,
            "-m",
            "local_agent.host_ops.capabilities.local.browser.playwright_worker_diagnostics",
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
        raise BrowserWorkerDiagnosticsError(
            f"browser worker diagnostics exceeded {timeout_seconds:g}s whole-process timeout"
        )
    if result.stdout_truncated or result.stderr_truncated:
        raise BrowserWorkerDiagnosticsError(
            "browser worker diagnostics helper output exceeded its bound"
        )

    payload = _parse_helper_json(result.stdout)
    if not result.ok:
        detail = payload.get("error") if isinstance(payload.get("error"), str) else None
        if detail:
            raise BrowserWorkerDiagnosticsError(_redact_attach_error(detail, endpoint))
        raise BrowserWorkerDiagnosticsError(
            f"browser worker diagnostics helper failed: {result.state.value} "
            f"exit={result.exit_code}"
        )
    return _diagnostics_from_payload(payload, expected_endpoint=endpoint)


def _parse_helper_json(raw: str) -> dict[str, Any]:
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise BrowserWorkerDiagnosticsError(
            "browser worker diagnostics helper returned invalid JSON"
        ) from exc
    if not isinstance(payload, dict):
        raise BrowserWorkerDiagnosticsError(
            "browser worker diagnostics helper returned a non-object JSON value"
        )
    return payload


def _sanitized_url(value: object, field: str, *, optional: bool = False) -> str | None:
    if value is None and optional:
        return None
    if not isinstance(value, str) or not value or _sanitize_target_url(value) != value:
        raise BrowserWorkerDiagnosticsError(
            f"browser worker diagnostics helper returned invalid {field}"
        )
    return value


def _optional_bool(value: object, field: str) -> bool | None:
    if value is None:
        return None
    if not isinstance(value, bool):
        raise BrowserWorkerDiagnosticsError(
            f"browser worker diagnostics helper returned invalid {field}"
        )
    return value


def _worker_target_from_payload(raw: object) -> BrowserWorkerTarget:
    if not isinstance(raw, dict) or set(raw) != _TARGET_KEYS:
        raise BrowserWorkerDiagnosticsError(
            "browser worker diagnostics helper returned an invalid worker target"
        )
    target_id = raw.get("id")
    target_type = raw.get("type")
    attached = raw.get("attached")
    try:
        validated_target_id = _validate_target_id(target_id) if isinstance(target_id, str) else None
    except ValueError as exc:
        raise BrowserWorkerDiagnosticsError(
            "browser worker diagnostics helper returned an invalid worker target id"
        ) from exc
    if validated_target_id is None:
        raise BrowserWorkerDiagnosticsError(
            "browser worker diagnostics helper returned an invalid worker target id"
        )
    if (
        not isinstance(target_type, str)
        or not 1 <= len(target_type) <= _MAX_TARGET_TYPE_CHARS
        or target_type not in _WORKER_TARGET_TYPES
    ):
        raise BrowserWorkerDiagnosticsError(
            "browser worker diagnostics helper returned an invalid worker target type"
        )
    if not isinstance(attached, bool):
        raise BrowserWorkerDiagnosticsError(
            "browser worker diagnostics helper returned an invalid worker attached state"
        )
    url = _sanitized_url(raw.get("url"), "worker target url")
    if url is None:
        raise BrowserWorkerDiagnosticsError(
            "browser worker diagnostics helper returned invalid worker target url"
        )
    return BrowserWorkerTarget(
        target_id=validated_target_id,
        target_type=target_type,
        url=url,
        attached=attached,
    )


def _registration_from_payload(raw: object) -> BrowserServiceWorkerRegistration:
    if not isinstance(raw, dict) or set(raw) != _REGISTRATION_KEYS:
        raise BrowserWorkerDiagnosticsError(
            "browser worker diagnostics helper returned an invalid registration"
        )
    scope_url = _sanitized_url(raw.get("scope_url"), "registration scope_url")
    is_deleted = raw.get("is_deleted")
    if not isinstance(is_deleted, bool):
        raise BrowserWorkerDiagnosticsError(
            "browser worker diagnostics helper returned an invalid registration deleted state"
        )
    if scope_url is None:
        raise BrowserWorkerDiagnosticsError(
            "browser worker diagnostics helper returned invalid registration scope_url"
        )
    return BrowserServiceWorkerRegistration(scope_url=scope_url, is_deleted=is_deleted)


def _version_from_payload(raw: object) -> BrowserServiceWorkerVersion:
    if not isinstance(raw, dict) or set(raw) != _VERSION_KEYS:
        raise BrowserWorkerDiagnosticsError(
            "browser worker diagnostics helper returned an invalid service worker version"
        )
    target_id_raw = raw.get("target_id")
    target_id: str | None = None
    if target_id_raw is not None:
        if not isinstance(target_id_raw, str):
            raise BrowserWorkerDiagnosticsError(
                "browser worker diagnostics helper returned an invalid version target id"
            )
        try:
            target_id = _validate_target_id(target_id_raw)
        except ValueError as exc:
            raise BrowserWorkerDiagnosticsError(
                "browser worker diagnostics helper returned an invalid version target id"
            ) from exc

    scope_url = _sanitized_url(raw.get("scope_url"), "version scope_url", optional=True)
    script_url = _sanitized_url(raw.get("script_url"), "version script_url")
    running_status = raw.get("running_status")
    lifecycle_status = raw.get("lifecycle_status")
    controlled_client_count = raw.get("controlled_client_count")
    if running_status not in _RUNNING_STATUSES:
        raise BrowserWorkerDiagnosticsError(
            "browser worker diagnostics helper returned an invalid running status"
        )
    if lifecycle_status not in _LIFECYCLE_STATUSES:
        raise BrowserWorkerDiagnosticsError(
            "browser worker diagnostics helper returned an invalid lifecycle status"
        )
    if (
        not isinstance(controlled_client_count, int)
        or isinstance(controlled_client_count, bool)
        or not 0 <= controlled_client_count <= _MAX_CONTROLLED_CLIENTS
    ):
        raise BrowserWorkerDiagnosticsError(
            "browser worker diagnostics helper returned an invalid controlled client count"
        )
    if script_url is None:
        raise BrowserWorkerDiagnosticsError(
            "browser worker diagnostics helper returned invalid version script_url"
        )
    return BrowserServiceWorkerVersion(
        target_id=target_id,
        scope_url=scope_url,
        script_url=script_url,
        running_status=running_status,
        lifecycle_status=lifecycle_status,
        controlled_client_count=controlled_client_count,
        target_attached=_optional_bool(raw.get("target_attached"), "target_attached"),
        registration_deleted=_optional_bool(
            raw.get("registration_deleted"), "registration_deleted"
        ),
    )


def _diagnostics_from_payload(
    payload: dict[str, Any], *, expected_endpoint: str
) -> BrowserWorkerDiagnostics:
    if set(payload) != _EXPECTED_PAYLOAD_KEYS:
        raise BrowserWorkerDiagnosticsError(
            "browser worker diagnostics helper returned an unexpected payload shape"
        )
    if payload.get("endpoint") != expected_endpoint:
        raise BrowserWorkerDiagnosticsError(
            "browser worker diagnostics helper returned the wrong endpoint"
        )

    worker_targets_raw = payload.get("worker_targets")
    registrations_raw = payload.get("registrations")
    versions_raw = payload.get("versions")
    error_count = payload.get("error_count")
    if not isinstance(worker_targets_raw, list) or len(worker_targets_raw) > _MAX_WORKER_TARGETS:
        raise BrowserWorkerDiagnosticsError(
            "browser worker diagnostics helper returned an invalid worker target list"
        )
    if not isinstance(registrations_raw, list) or len(registrations_raw) > _MAX_REGISTRATIONS:
        raise BrowserWorkerDiagnosticsError(
            "browser worker diagnostics helper returned an invalid registration list"
        )
    if not isinstance(versions_raw, list) or len(versions_raw) > _MAX_VERSIONS:
        raise BrowserWorkerDiagnosticsError(
            "browser worker diagnostics helper returned an invalid version list"
        )
    if (
        not isinstance(error_count, int)
        or isinstance(error_count, bool)
        or not 0 <= error_count <= _MAX_ERROR_COUNT
    ):
        raise BrowserWorkerDiagnosticsError(
            "browser worker diagnostics helper returned an invalid error count"
        )

    return BrowserWorkerDiagnostics(
        endpoint=expected_endpoint,
        worker_targets=tuple(_worker_target_from_payload(item) for item in worker_targets_raw),
        registrations=tuple(_registration_from_payload(item) for item in registrations_raw),
        versions=tuple(_version_from_payload(item) for item in versions_raw),
        error_count=error_count,
    )
