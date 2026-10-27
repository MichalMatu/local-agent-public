from __future__ import annotations

import importlib
import json

import pytest

from local_agent.host_ops.capabilities.local.browser import (
    BrowserCdpWorkerDiagnoser,
    BrowserServiceWorkerRegistration,
    BrowserServiceWorkerVersion,
    BrowserWorkerDiagnostics,
    BrowserWorkerDiagnosticsError,
    BrowserWorkerTarget,
)
from local_agent.host_ops.core.execution import ProcessResult, ProcessState

worker_diagnostics = importlib.import_module(
    "local_agent.host_ops.capabilities.local.browser.worker_diagnostics"
)


def _evidence() -> BrowserWorkerDiagnostics:
    return BrowserWorkerDiagnostics(
        endpoint="http://127.0.0.1:9222",
        worker_targets=(
            BrowserWorkerTarget(
                target_id="worker-1",
                target_type="service_worker",
                url="chrome-extension:",
                attached=False,
            ),
        ),
        registrations=(
            BrowserServiceWorkerRegistration(scope_url="chrome-extension:", is_deleted=False),
        ),
        versions=(
            BrowserServiceWorkerVersion(
                target_id="worker-1",
                scope_url="chrome-extension:",
                script_url="chrome-extension:",
                running_status="running",
                lifecycle_status="activated",
                controlled_client_count=1,
                target_attached=False,
                registration_deleted=False,
            ),
        ),
        error_count=0,
    )


def _process_result(
    *,
    stdout: str = "",
    stderr: str = "",
    state: ProcessState = ProcessState.COMPLETED,
    exit_code: int | None = 0,
    stdout_truncated: bool = False,
    stderr_truncated: bool = False,
) -> ProcessResult:
    return ProcessResult(
        state=state,
        exit_code=exit_code,
        stdout=stdout,
        stderr=stderr,
        duration_seconds=0.1,
        stdout_truncated=stdout_truncated,
        stderr_truncated=stderr_truncated,
    )


def test_worker_diagnostics_validates_endpoint_and_timeout_before_backend() -> None:
    calls: list[tuple[str, float]] = []

    def backend(endpoint: str, timeout_seconds: float) -> BrowserWorkerDiagnostics:
        calls.append((endpoint, timeout_seconds))
        return _evidence()

    result = BrowserCdpWorkerDiagnoser(backend).inspect(
        " http://127.0.0.1:9222/ ", timeout_seconds=7.0
    )

    assert result == _evidence()
    assert calls == [("http://127.0.0.1:9222", 7.0)]

    with pytest.raises(ValueError, match="browser endpoint"):
        BrowserCdpWorkerDiagnoser(backend).inspect("https://example.test:9222")
    with pytest.raises(ValueError, match="timeout_seconds"):
        BrowserCdpWorkerDiagnoser(backend).inspect("http://127.0.0.1:9222", timeout_seconds=0.5)


def test_worker_diagnostics_uses_bounded_helper_and_keeps_endpoint_out_of_argv(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    endpoint = "http://127.0.0.1:9222"
    calls = []

    class FakeRunner:
        def run(self, argv, *, cwd=None, env_overrides=None, limits=None):
            assert cwd is None
            calls.append((list(argv), dict(env_overrides or {}), limits))
            return _process_result(stdout=json.dumps(_evidence().as_dict()))

    monkeypatch.setattr(worker_diagnostics, "ProcessRunner", FakeRunner)

    result = BrowserCdpWorkerDiagnoser().inspect(endpoint, timeout_seconds=8.0)

    assert result == _evidence()
    argv, environment, limits = calls[0]
    assert argv[2] == (
        "local_agent.host_ops.capabilities.local.browser.playwright_worker_diagnostics"
    )
    assert endpoint not in argv
    assert environment == {worker_diagnostics._ATTACH_ENDPOINT_ENV: endpoint}
    assert limits.timeout_seconds == 8.0
    assert limits.max_stdout_bytes == worker_diagnostics._MAX_HELPER_OUTPUT_BYTES
    assert limits.max_stderr_bytes == worker_diagnostics._MAX_HELPER_OUTPUT_BYTES


def test_worker_diagnostics_timeout_and_truncation_do_not_expose_child_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class TimedOutRunner:
        def run(self, argv, *, cwd=None, env_overrides=None, limits=None):
            return _process_result(
                state=ProcessState.TIMED_OUT,
                exit_code=None,
                stdout="private stdout",
                stderr="private stderr",
            )

    monkeypatch.setattr(worker_diagnostics, "ProcessRunner", TimedOutRunner)
    with pytest.raises(BrowserWorkerDiagnosticsError, match="whole-process timeout") as caught:
        BrowserCdpWorkerDiagnoser().inspect("http://127.0.0.1:9222")
    assert "private" not in str(caught.value)

    class TruncatedRunner:
        def run(self, argv, *, cwd=None, env_overrides=None, limits=None):
            return _process_result(stdout=json.dumps(_evidence().as_dict()), stdout_truncated=True)

    monkeypatch.setattr(worker_diagnostics, "ProcessRunner", TruncatedRunner)
    with pytest.raises(BrowserWorkerDiagnosticsError, match="output exceeded"):
        BrowserCdpWorkerDiagnoser().inspect("http://127.0.0.1:9222")


def test_worker_diagnostics_strictly_revalidates_helper_payload() -> None:
    payload = _evidence().as_dict()
    payload["extra"] = "not allowed"
    with pytest.raises(BrowserWorkerDiagnosticsError, match="unexpected payload shape"):
        worker_diagnostics._diagnostics_from_payload(
            payload, expected_endpoint="http://127.0.0.1:9222"
        )

    payload = _evidence().as_dict()
    payload["worker_targets"][0]["url"] = "chrome-extension://secret-id/background.js"
    with pytest.raises(BrowserWorkerDiagnosticsError, match="worker target url"):
        worker_diagnostics._diagnostics_from_payload(
            payload, expected_endpoint="http://127.0.0.1:9222"
        )

    payload = _evidence().as_dict()
    payload["versions"][0]["running_status"] = "mystery"
    with pytest.raises(BrowserWorkerDiagnosticsError, match="running status"):
        worker_diagnostics._diagnostics_from_payload(
            payload, expected_endpoint="http://127.0.0.1:9222"
        )


def test_worker_diagnostics_model_contains_no_registration_version_or_client_ids() -> None:
    payload = _evidence().as_dict()
    encoded = json.dumps(payload)

    assert set(payload) == {
        "endpoint",
        "worker_targets",
        "registrations",
        "versions",
        "error_count",
    }
    assert "registration_id" not in encoded
    assert "version_id" not in encoded
    assert "controlled_client_ids" not in encoded
    assert "error_message" not in encoded
