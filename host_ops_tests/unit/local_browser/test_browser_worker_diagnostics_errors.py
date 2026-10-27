from __future__ import annotations

import importlib
import json
from types import SimpleNamespace

import pytest

from local_agent.host_ops.capabilities.local.browser import BrowserWorkerDiagnosticsError
from local_agent.host_ops.core.execution import ProcessState

worker_diagnostics = importlib.import_module(
    "local_agent.host_ops.capabilities.local.browser.worker_diagnostics"
)

_ENDPOINT = "http://127.0.0.1:9222"


def _payload() -> dict:
    return {
        "endpoint": _ENDPOINT,
        "worker_targets": [
            {
                "id": "worker-1",
                "type": "service_worker",
                "url": "chrome-extension:",
                "attached": False,
            }
        ],
        "registrations": [{"scope_url": "chrome-extension:", "is_deleted": False}],
        "versions": [
            {
                "target_id": "worker-1",
                "scope_url": "chrome-extension:",
                "script_url": "chrome-extension:",
                "running_status": "running",
                "lifecycle_status": "activated",
                "controlled_client_count": 1,
                "target_attached": False,
                "registration_deleted": False,
            }
        ],
        "error_count": 0,
    }


def _runner_result(**overrides):
    values = {
        "state": ProcessState.COMPLETED,
        "stdout_truncated": False,
        "stderr_truncated": False,
        "stdout": "{}",
        "ok": True,
        "exit_code": 0,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_helper_failure_uses_redacted_detail_and_generic_fallback(monkeypatch) -> None:
    class DetailRunner:
        def run(self, *args, **kwargs):
            return _runner_result(
                stdout=json.dumps({"error": f"failed at {_ENDPOINT}/private?token=x"}),
                ok=False,
                exit_code=1,
            )

    monkeypatch.setattr(worker_diagnostics, "ProcessRunner", DetailRunner)
    with pytest.raises(BrowserWorkerDiagnosticsError) as caught:
        worker_diagnostics._inspect_with_bounded_helper(_ENDPOINT, 3.0)
    assert "token=x" not in str(caught.value)

    class GenericRunner:
        def run(self, *args, **kwargs):
            return _runner_result(stdout="{}", ok=False, exit_code=7)

    monkeypatch.setattr(worker_diagnostics, "ProcessRunner", GenericRunner)
    with pytest.raises(BrowserWorkerDiagnosticsError, match="exit=7"):
        worker_diagnostics._inspect_with_bounded_helper(_ENDPOINT, 3.0)


def test_helper_json_parser_rejects_invalid_and_non_object_values() -> None:
    with pytest.raises(BrowserWorkerDiagnosticsError, match="invalid JSON"):
        worker_diagnostics._parse_helper_json("not-json")
    with pytest.raises(BrowserWorkerDiagnosticsError, match="non-object"):
        worker_diagnostics._parse_helper_json("[]")


def test_payload_rejects_wrong_endpoint_and_invalid_collection_types() -> None:
    payload = _payload()
    payload["endpoint"] = "http://127.0.0.1:9333"
    with pytest.raises(BrowserWorkerDiagnosticsError, match="wrong endpoint"):
        worker_diagnostics._diagnostics_from_payload(payload, expected_endpoint=_ENDPOINT)

    for key in ("worker_targets", "registrations", "versions"):
        payload = _payload()
        payload[key] = {}
        with pytest.raises(BrowserWorkerDiagnosticsError, match="invalid"):
            worker_diagnostics._diagnostics_from_payload(payload, expected_endpoint=_ENDPOINT)

    payload = _payload()
    payload["error_count"] = True
    with pytest.raises(BrowserWorkerDiagnosticsError, match="invalid error count"):
        worker_diagnostics._diagnostics_from_payload(payload, expected_endpoint=_ENDPOINT)


def test_worker_target_validation_rejects_shape_id_type_and_attached_state() -> None:
    with pytest.raises(BrowserWorkerDiagnosticsError, match="invalid worker target"):
        worker_diagnostics._worker_target_from_payload([])

    payload = _payload()["worker_targets"][0]
    for key, value, message in (
        ("id", "bad id!", "target id"),
        ("type", "page", "target type"),
        ("attached", "no", "attached state"),
    ):
        item = dict(payload)
        item[key] = value
        with pytest.raises(BrowserWorkerDiagnosticsError, match=message):
            worker_diagnostics._worker_target_from_payload(item)


def test_registration_validation_rejects_shape_url_and_deleted_state() -> None:
    with pytest.raises(BrowserWorkerDiagnosticsError, match="invalid registration"):
        worker_diagnostics._registration_from_payload({})

    item = {"scope_url": "chrome-extension://secret/path", "is_deleted": False}
    with pytest.raises(BrowserWorkerDiagnosticsError, match="scope_url"):
        worker_diagnostics._registration_from_payload(item)

    item = {"scope_url": "chrome-extension:", "is_deleted": 1}
    with pytest.raises(BrowserWorkerDiagnosticsError, match="deleted state"):
        worker_diagnostics._registration_from_payload(item)


def test_version_validation_rejects_ids_status_counts_and_optional_bools() -> None:
    base = _payload()["versions"][0]
    cases = (
        ("target_id", 4, "target id"),
        ("target_id", "bad id!", "target id"),
        ("script_url", "chrome-extension://secret/path", "script_url"),
        ("running_status", "awake", "running status"),
        ("lifecycle_status", "ready", "lifecycle status"),
        ("controlled_client_count", True, "client count"),
        ("controlled_client_count", -1, "client count"),
        ("target_attached", "no", "target_attached"),
        ("registration_deleted", 0, "registration_deleted"),
    )
    for key, value, message in cases:
        item = dict(base)
        item[key] = value
        with pytest.raises(BrowserWorkerDiagnosticsError, match=message):
            worker_diagnostics._version_from_payload(item)


def test_optional_scope_can_be_none_but_unsanitized_scope_fails() -> None:
    item = dict(_payload()["versions"][0])
    item["scope_url"] = None
    assert worker_diagnostics._version_from_payload(item).scope_url is None

    item["scope_url"] = "https://example.test/path?secret=x"
    with pytest.raises(BrowserWorkerDiagnosticsError, match="scope_url"):
        worker_diagnostics._version_from_payload(item)
