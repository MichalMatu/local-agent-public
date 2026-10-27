"""Structured local browser inspection, probing and attach evidence."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class BrowserProcess:
    pid: int
    parent_pid: int
    family: str
    remote_debugging_port: int | None
    remote_debugging_address: str | None
    remote_debugging_pipe: bool
    user_data_dir: str | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "pid": self.pid,
            "parent_pid": self.parent_pid,
            "family": self.family,
            "remote_debugging_port": self.remote_debugging_port,
            "remote_debugging_address": self.remote_debugging_address,
            "remote_debugging_pipe": self.remote_debugging_pipe,
            "user_data_dir": self.user_data_dir,
        }


@dataclass(frozen=True, slots=True)
class DevtoolsTarget:
    target_id: str
    target_type: str
    title: str
    url: str
    web_socket_debugger_url: str | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.target_id,
            "type": self.target_type,
            "title": self.title,
            "url": self.url,
            "web_socket_debugger_url": self.web_socket_debugger_url,
        }


@dataclass(frozen=True, slots=True)
class DevtoolsEndpoint:
    endpoint: str
    reachable: bool
    browser: str | None
    protocol_version: str | None
    user_agent: str | None
    web_socket_debugger_url: str | None
    targets: tuple[DevtoolsTarget, ...]
    error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "endpoint": self.endpoint,
            "reachable": self.reachable,
            "browser": self.browser,
            "protocol_version": self.protocol_version,
            "user_agent": self.user_agent,
            "web_socket_debugger_url": self.web_socket_debugger_url,
            "targets": [target.as_dict() for target in self.targets],
            "error": self.error,
        }


@dataclass(frozen=True, slots=True)
class BrowserInspection:
    processes: tuple[BrowserProcess, ...]
    endpoints: tuple[DevtoolsEndpoint, ...]
    warnings: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "processes": [process.as_dict() for process in self.processes],
            "endpoints": [endpoint.as_dict() for endpoint in self.endpoints],
            "warnings": list(self.warnings),
        }


@dataclass(frozen=True, slots=True)
class ManagedBrowserProbe:
    engine: str
    requested_url: str
    final_url: str
    title: str
    status_code: int | None
    console_error_count: int
    page_error_count: int
    request_failure_count: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "engine": self.engine,
            "requested_url": self.requested_url,
            "final_url": self.final_url,
            "title": self.title,
            "status_code": self.status_code,
            "console_error_count": self.console_error_count,
            "page_error_count": self.page_error_count,
            "request_failure_count": self.request_failure_count,
        }


@dataclass(frozen=True, slots=True)
class ManagedBrowserSession:
    state: str
    pid: int | None
    endpoint: str | None
    browser: str | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "pid": self.pid,
            "endpoint": self.endpoint,
            "browser": self.browser,
        }


@dataclass(frozen=True, slots=True)
class BrowserAttachTarget:
    target_id: str
    target_type: str
    title: str
    url: str
    attached: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.target_id,
            "type": self.target_type,
            "title": self.title,
            "url": self.url,
            "attached": self.attached,
        }


@dataclass(frozen=True, slots=True)
class BrowserAttachInspection:
    endpoint: str
    browser_version: str
    context_count: int
    targets: tuple[BrowserAttachTarget, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "endpoint": self.endpoint,
            "browser_version": self.browser_version,
            "context_count": self.context_count,
            "targets": [target.as_dict() for target in self.targets],
        }


@dataclass(frozen=True, slots=True)
class BrowserPageSnapshot:
    endpoint: str
    target_id: str
    target_type: str
    title: str
    url: str
    frame_count: int
    main_frame_url: str
    main_frame_mime_type: str
    document_node_name: str
    document_child_count: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "endpoint": self.endpoint,
            "target_id": self.target_id,
            "target_type": self.target_type,
            "title": self.title,
            "url": self.url,
            "frame_count": self.frame_count,
            "main_frame_url": self.main_frame_url,
            "main_frame_mime_type": self.main_frame_mime_type,
            "document_node_name": self.document_node_name,
            "document_child_count": self.document_child_count,
        }


@dataclass(frozen=True, slots=True)
class BrowserSelectorCount:
    selector: str
    match_count: int

    def as_dict(self) -> dict[str, Any]:
        return {"selector": self.selector, "match_count": self.match_count}


@dataclass(frozen=True, slots=True)
class BrowserSelectorInspection:
    endpoint: str
    target_id: str
    target_type: str
    title: str
    url: str
    selectors: tuple[BrowserSelectorCount, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "endpoint": self.endpoint,
            "target_id": self.target_id,
            "target_type": self.target_type,
            "title": self.title,
            "url": self.url,
            "selectors": [item.as_dict() for item in self.selectors],
        }


@dataclass(frozen=True, slots=True)
class BrowserReloadResult:
    endpoint: str
    target_id: str
    target_type: str
    expected_url: str
    before_url: str
    before_title: str
    after_url: str
    after_title: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "endpoint": self.endpoint,
            "target_id": self.target_id,
            "target_type": self.target_type,
            "expected_url": self.expected_url,
            "before_url": self.before_url,
            "before_title": self.before_title,
            "after_url": self.after_url,
            "after_title": self.after_title,
        }


@dataclass(frozen=True, slots=True)
class BrowserWorkerTarget:
    target_id: str
    target_type: str
    url: str
    attached: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.target_id,
            "type": self.target_type,
            "url": self.url,
            "attached": self.attached,
        }


@dataclass(frozen=True, slots=True)
class BrowserServiceWorkerRegistration:
    scope_url: str
    is_deleted: bool

    def as_dict(self) -> dict[str, Any]:
        return {"scope_url": self.scope_url, "is_deleted": self.is_deleted}


@dataclass(frozen=True, slots=True)
class BrowserServiceWorkerVersion:
    target_id: str | None
    scope_url: str | None
    script_url: str
    running_status: str
    lifecycle_status: str
    controlled_client_count: int
    target_attached: bool | None
    registration_deleted: bool | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "target_id": self.target_id,
            "scope_url": self.scope_url,
            "script_url": self.script_url,
            "running_status": self.running_status,
            "lifecycle_status": self.lifecycle_status,
            "controlled_client_count": self.controlled_client_count,
            "target_attached": self.target_attached,
            "registration_deleted": self.registration_deleted,
        }


@dataclass(frozen=True, slots=True)
class BrowserWorkerDiagnostics:
    endpoint: str
    worker_targets: tuple[BrowserWorkerTarget, ...]
    registrations: tuple[BrowserServiceWorkerRegistration, ...]
    versions: tuple[BrowserServiceWorkerVersion, ...]
    error_count: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "endpoint": self.endpoint,
            "worker_targets": [target.as_dict() for target in self.worker_targets],
            "registrations": [registration.as_dict() for registration in self.registrations],
            "versions": [version.as_dict() for version in self.versions],
            "error_count": self.error_count,
        }


@dataclass(frozen=True, slots=True)
class BrowserExtensionScriptEvidence:
    name: str
    status: str

    def as_dict(self) -> dict[str, Any]:
        return {"name": self.name, "status": self.status}


@dataclass(frozen=True, slots=True)
class BrowserReadinessInspection:
    endpoint: str
    target_id: str
    target_type: str
    title: str
    url: str
    selectors: tuple[BrowserSelectorCount, ...]
    extension_scripts: tuple[BrowserExtensionScriptEvidence, ...]
    dom_ready: bool
    content_script_state: str
    worker_state: str
    diagnosis: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "endpoint": self.endpoint,
            "target_id": self.target_id,
            "target_type": self.target_type,
            "title": self.title,
            "url": self.url,
            "selectors": [item.as_dict() for item in self.selectors],
            "extension_scripts": [item.as_dict() for item in self.extension_scripts],
            "dom_ready": self.dom_ready,
            "content_script_state": self.content_script_state,
            "worker_state": self.worker_state,
            "diagnosis": self.diagnosis,
        }


@dataclass(frozen=True, slots=True)
class BrowserContentScriptRecoveryResult:
    endpoint: str
    target_id: str
    expected_url: str
    action: str
    outcome: str
    before: BrowserReadinessInspection
    reload: BrowserReloadResult | None
    after: BrowserReadinessInspection | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "endpoint": self.endpoint,
            "target_id": self.target_id,
            "expected_url": self.expected_url,
            "action": self.action,
            "outcome": self.outcome,
            "before": self.before.as_dict(),
            "reload": self.reload.as_dict() if self.reload is not None else None,
            "after": self.after.as_dict() if self.after is not None else None,
        }
