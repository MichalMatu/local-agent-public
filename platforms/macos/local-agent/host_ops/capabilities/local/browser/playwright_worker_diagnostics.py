"""Internal Playwright helper for read-only Chromium worker diagnostics."""

from __future__ import annotations

import argparse
import importlib
import json
import os
from typing import Any, cast

from .attach import _ATTACH_ENDPOINT_ENV, BrowserAttachError
from .inspection import _sanitize_target_url
from .models import (
    BrowserServiceWorkerRegistration,
    BrowserServiceWorkerVersion,
    BrowserWorkerDiagnostics,
    BrowserWorkerTarget,
)
from .snapshot import _validate_target_id
from .worker_diagnostics import (
    _LIFECYCLE_STATUSES,
    _MAX_CONTROLLED_CLIENTS,
    _MAX_ERROR_COUNT,
    _MAX_REGISTRATIONS,
    _MAX_TARGET_TYPE_CHARS,
    _MAX_VERSIONS,
    _MAX_WORKER_TARGETS,
    _RUNNING_STATUSES,
    _WORKER_TARGET_TYPES,
    BrowserWorkerDiagnosticsError,
)

_MAX_PAGE_SESSIONS = 128
_EVENT_DRAIN_MS = 300


def _load_playwright_sync_api() -> Any:
    try:
        return importlib.import_module("playwright.sync_api")
    except ModuleNotFoundError as exc:
        if exc.name == "playwright" or str(exc.name).startswith("playwright."):
            raise BrowserWorkerDiagnosticsError(
                "Playwright is not installed; install the optional 'host-ops[browser]' extra"
            ) from exc
        raise


def _operation_timeout_ms(timeout_seconds: float) -> float:
    cleanup_reserve = min(1.0, timeout_seconds / 4.0)
    return max(250.0, (timeout_seconds - cleanup_reserve) * 1000.0)


def _bounded_id(value: object, field: str) -> str:
    if not isinstance(value, str):
        raise BrowserWorkerDiagnosticsError(f"CDP {field} is invalid")
    try:
        return _validate_target_id(value)
    except ValueError as exc:
        raise BrowserWorkerDiagnosticsError(f"CDP {field} is invalid") from exc


def _sanitized_url(value: object, field: str) -> str:
    raw = value if isinstance(value, str) else ""
    sanitized = _sanitize_target_url(raw)
    if not sanitized:
        raise BrowserWorkerDiagnosticsError(f"CDP {field} is invalid")
    return sanitized


def _worker_targets_from_response(response: object) -> tuple[BrowserWorkerTarget, ...]:
    if not isinstance(response, dict) or not isinstance(response.get("targetInfos"), list):
        raise BrowserWorkerDiagnosticsError("CDP Target.getTargets returned invalid target data")
    raw_targets = response["targetInfos"]
    if len(raw_targets) > 4096:
        raise BrowserWorkerDiagnosticsError("CDP target inventory exceeded its bound")

    targets: list[BrowserWorkerTarget] = []
    for raw in raw_targets:
        if not isinstance(raw, dict):
            raise BrowserWorkerDiagnosticsError("CDP target inventory contains an invalid target")
        target_type = raw.get("type")
        if not isinstance(target_type, str) or not 1 <= len(target_type) <= _MAX_TARGET_TYPE_CHARS:
            raise BrowserWorkerDiagnosticsError("CDP target has an invalid type")
        if target_type not in _WORKER_TARGET_TYPES:
            continue
        attached = raw.get("attached")
        if not isinstance(attached, bool):
            raise BrowserWorkerDiagnosticsError("CDP worker target has an invalid attached state")
        targets.append(
            BrowserWorkerTarget(
                target_id=_bounded_id(raw.get("targetId"), "worker target id"),
                target_type=target_type,
                url=_sanitized_url(raw.get("url"), "worker target URL"),
                attached=attached,
            )
        )
        if len(targets) > _MAX_WORKER_TARGETS:
            raise BrowserWorkerDiagnosticsError("CDP worker target inventory exceeded its bound")
    return tuple(sorted(targets, key=lambda item: (item.target_type, item.target_id)))


def _merge_registrations(
    event: object, registrations: dict[str, BrowserServiceWorkerRegistration]
) -> None:
    if not isinstance(event, dict) or not isinstance(event.get("registrations"), list):
        raise BrowserWorkerDiagnosticsError("CDP ServiceWorker registration event is invalid")
    for raw in event["registrations"]:
        if not isinstance(raw, dict):
            raise BrowserWorkerDiagnosticsError("CDP ServiceWorker registration is invalid")
        registration_id = _bounded_id(raw.get("registrationId"), "registration id")
        is_deleted = raw.get("isDeleted")
        if not isinstance(is_deleted, bool):
            raise BrowserWorkerDiagnosticsError(
                "CDP ServiceWorker registration deleted state is invalid"
            )
        registrations[registration_id] = BrowserServiceWorkerRegistration(
            scope_url=_sanitized_url(raw.get("scopeURL"), "registration scope URL"),
            is_deleted=is_deleted,
        )
        if len(registrations) > _MAX_REGISTRATIONS:
            raise BrowserWorkerDiagnosticsError(
                "CDP ServiceWorker registrations exceeded their bound"
            )


def _merge_versions(event: object, versions: dict[str, dict[str, object]]) -> None:
    if not isinstance(event, dict) or not isinstance(event.get("versions"), list):
        raise BrowserWorkerDiagnosticsError("CDP ServiceWorker version event is invalid")
    for raw in event["versions"]:
        if not isinstance(raw, dict):
            raise BrowserWorkerDiagnosticsError("CDP ServiceWorker version is invalid")
        version_id = _bounded_id(raw.get("versionId"), "version id")
        registration_id = _bounded_id(raw.get("registrationId"), "version registration id")
        running_status = raw.get("runningStatus")
        lifecycle_status = raw.get("status")
        if running_status not in _RUNNING_STATUSES:
            raise BrowserWorkerDiagnosticsError("CDP ServiceWorker running status is invalid")
        if lifecycle_status not in _LIFECYCLE_STATUSES:
            raise BrowserWorkerDiagnosticsError("CDP ServiceWorker lifecycle status is invalid")

        controlled_clients = raw.get("controlledClients", [])
        if (
            not isinstance(controlled_clients, list)
            or len(controlled_clients) > _MAX_CONTROLLED_CLIENTS
        ):
            raise BrowserWorkerDiagnosticsError("CDP ServiceWorker controlled clients are invalid")
        for client_id in controlled_clients:
            _bounded_id(client_id, "controlled client id")

        target_id_raw = raw.get("targetId")
        target_id = None
        if target_id_raw is not None:
            target_id = _bounded_id(target_id_raw, "version target id")

        versions[version_id] = {
            "registration_id": registration_id,
            "target_id": target_id,
            "script_url": _sanitized_url(raw.get("scriptURL"), "version script URL"),
            "running_status": running_status,
            "lifecycle_status": lifecycle_status,
            "controlled_client_count": len(controlled_clients),
        }
        if len(versions) > _MAX_VERSIONS:
            raise BrowserWorkerDiagnosticsError("CDP ServiceWorker versions exceeded their bound")


def _versions_from_state(
    versions: dict[str, dict[str, object]],
    registrations: dict[str, BrowserServiceWorkerRegistration],
    worker_targets: tuple[BrowserWorkerTarget, ...],
) -> tuple[BrowserServiceWorkerVersion, ...]:
    attached_by_target = {target.target_id: target.attached for target in worker_targets}
    result: list[BrowserServiceWorkerVersion] = []
    for version_id in sorted(versions):
        raw = versions[version_id]
        registration_id = raw["registration_id"]
        target_id = raw["target_id"]
        registration = (
            registrations.get(registration_id) if isinstance(registration_id, str) else None
        )
        result.append(
            BrowserServiceWorkerVersion(
                target_id=target_id if isinstance(target_id, str) else None,
                scope_url=registration.scope_url if registration is not None else None,
                script_url=str(raw["script_url"]),
                running_status=str(raw["running_status"]),
                lifecycle_status=str(raw["lifecycle_status"]),
                controlled_client_count=cast(int, raw["controlled_client_count"]),
                target_attached=(
                    attached_by_target.get(target_id) if isinstance(target_id, str) else None
                ),
                registration_deleted=(
                    registration.is_deleted if registration is not None else None
                ),
            )
        )
    return tuple(result)


def _run_worker_diagnostics(endpoint: str, timeout_seconds: float) -> BrowserWorkerDiagnostics:
    sync_api = _load_playwright_sync_api()
    timeout_ms = _operation_timeout_ms(timeout_seconds)

    with sync_api.sync_playwright() as playwright:
        browser = playwright.chromium.connect_over_cdp(
            endpoint, timeout=timeout_ms, is_local=True, no_defaults=True
        )
        browser_session = None
        page_sessions: list[tuple[Any, bool]] = []
        registrations: dict[str, BrowserServiceWorkerRegistration] = {}
        versions: dict[str, dict[str, object]] = {}
        event_errors: list[str] = []
        error_count = 0

        def on_registrations(event: object) -> None:
            try:
                _merge_registrations(event, registrations)
            except BrowserWorkerDiagnosticsError as exc:
                event_errors.append(str(exc))

        def on_versions(event: object) -> None:
            try:
                _merge_versions(event, versions)
            except BrowserWorkerDiagnosticsError as exc:
                event_errors.append(str(exc))

        def on_error(event: object) -> None:
            nonlocal error_count
            if not isinstance(event, dict) or not isinstance(event.get("errorMessage"), dict):
                event_errors.append("CDP ServiceWorker error event is invalid")
                return
            error_count += 1
            if error_count > _MAX_ERROR_COUNT:
                event_errors.append("CDP ServiceWorker error count exceeded its bound")

        try:
            browser_session = browser.new_browser_cdp_session()
            pages: list[tuple[Any, Any]] = []
            for context in browser.contexts:
                for page in context.pages:
                    pages.append((context, page))
                    if len(pages) > _MAX_PAGE_SESSIONS:
                        raise BrowserWorkerDiagnosticsError(
                            "Chromium page inventory exceeded the worker diagnostics bound"
                        )

            for context, page in pages:
                session = context.new_cdp_session(page)
                page_sessions.append((session, False))
                session.on("ServiceWorker.workerRegistrationUpdated", on_registrations)
                session.on("ServiceWorker.workerVersionUpdated", on_versions)
                session.on("ServiceWorker.workerErrorReported", on_error)
                try:
                    session.send("ServiceWorker.enable")
                except Exception as exc:
                    raise BrowserWorkerDiagnosticsError(
                        "CDP ServiceWorker diagnostics are unavailable for an attached page"
                    ) from exc
                page_sessions[-1] = (session, True)

            if pages:
                pages[0][1].wait_for_timeout(_EVENT_DRAIN_MS)

            target_response = browser_session.send("Target.getTargets")
            if event_errors:
                raise BrowserWorkerDiagnosticsError(event_errors[0])
            worker_targets = _worker_targets_from_response(target_response)
            return BrowserWorkerDiagnostics(
                endpoint=endpoint,
                worker_targets=worker_targets,
                registrations=tuple(
                    registration for _, registration in sorted(registrations.items())
                ),
                versions=_versions_from_state(versions, registrations, worker_targets),
                error_count=error_count,
            )
        finally:
            try:
                for session, service_worker_enabled in reversed(page_sessions):
                    try:
                        if service_worker_enabled:
                            session.send("ServiceWorker.disable")
                    finally:
                        session.detach()
            finally:
                try:
                    if browser_session is not None:
                        browser_session.detach()
                finally:
                    browser.close(reason="host-ops read-only worker diagnostics complete")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--timeout", type=float, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    endpoint = os.environ.get(_ATTACH_ENDPOINT_ENV, "").strip()
    if not endpoint:
        print(
            json.dumps({"error": "browser worker diagnostics endpoint is missing"}, sort_keys=True)
        )
        return 2
    if not 1 <= args.timeout <= 120:
        print(
            json.dumps({"error": "browser worker diagnostics timeout is invalid"}, sort_keys=True)
        )
        return 2

    try:
        result = _run_worker_diagnostics(endpoint, args.timeout)
    except (BrowserWorkerDiagnosticsError, BrowserAttachError) as exc:
        print(json.dumps({"error": str(exc)[:1000]}, sort_keys=True))
        return 1
    except Exception:
        print(json.dumps({"error": "browser worker diagnostics failed"}, sort_keys=True))
        return 1

    print(json.dumps(result.as_dict(), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
