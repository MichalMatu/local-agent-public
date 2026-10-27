from __future__ import annotations

import importlib
import json

import pytest

from local_agent.host_ops.capabilities.local.browser import (
    BrowserAttachError,
    BrowserAttachInspection,
    BrowserAttachTarget,
    BrowserCdpAttacher,
)
from local_agent.host_ops.core.execution import ProcessResult, ProcessState

attach = importlib.import_module("local_agent.host_ops.capabilities.local.browser.attach")


def _evidence() -> BrowserAttachInspection:
    return BrowserAttachInspection(
        endpoint="http://127.0.0.1:9222",
        browser_version="153.0.8010.12",
        context_count=1,
        targets=(
            BrowserAttachTarget(
                target_id="page-1",
                target_type="page",
                title="Example",
                url="https://example.test/path",
                attached=False,
            ),
        ),
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


def test_attach_validates_and_normalizes_loopback_endpoint_before_backend() -> None:
    calls: list[tuple[str, float]] = []

    def backend(endpoint: str, timeout_seconds: float) -> BrowserAttachInspection:
        calls.append((endpoint, timeout_seconds))
        return _evidence()

    result = BrowserCdpAttacher(backend).inspect(
        " http://127.0.0.1:9222/ ",
        timeout_seconds=4.5,
    )

    assert result == _evidence()
    assert calls == [("http://127.0.0.1:9222", 4.5)]


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://example.com:9222",
        "http://user:pass@127.0.0.1:9222",
        "http://127.0.0.1:9222/json/list",
        "http://127.0.0.1",
        "ws://127.0.0.1:9222/devtools/browser/id",
    ],
)
def test_attach_rejects_non_loopback_or_non_base_endpoints(endpoint: str) -> None:
    with pytest.raises(ValueError, match="browser endpoint"):
        BrowserCdpAttacher(lambda *_args: _evidence()).inspect(endpoint)


@pytest.mark.parametrize("timeout_seconds", [0.0, 0.5, -1.0, 120.1])
def test_attach_rejects_invalid_timeout(timeout_seconds: float) -> None:
    with pytest.raises(ValueError, match="timeout_seconds"):
        BrowserCdpAttacher(lambda *_args: _evidence()).inspect(
            "http://127.0.0.1:9222",
            timeout_seconds=timeout_seconds,
        )


def test_bounded_attach_uses_core_runner_and_keeps_endpoint_out_of_argv(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    endpoint = "http://127.0.0.1:9222"
    calls: list[tuple[list[str], dict[str, str], object]] = []

    class FakeRunner:
        def run(self, argv, *, cwd=None, env_overrides=None, limits=None):
            assert cwd is None
            calls.append((list(argv), dict(env_overrides or {}), limits))
            return _process_result(stdout=json.dumps(_evidence().as_dict()))

    monkeypatch.setattr(attach, "ProcessRunner", FakeRunner)

    result = BrowserCdpAttacher().inspect(endpoint, timeout_seconds=7.0)

    assert result == _evidence()
    argv, environment, limits = calls[0]
    assert argv[2] == (
        "local_agent.host_ops.capabilities.local.browser.playwright_attach"
    )
    assert endpoint not in argv
    assert environment == {attach._ATTACH_ENDPOINT_ENV: endpoint}
    assert limits.timeout_seconds == 7.0
    assert limits.max_stdout_bytes == attach._MAX_HELPER_OUTPUT_BYTES
    assert limits.max_stderr_bytes == attach._MAX_HELPER_OUTPUT_BYTES


def test_attach_timeout_does_not_expose_child_output(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeRunner:
        def run(self, argv, *, cwd=None, env_overrides=None, limits=None):
            return _process_result(
                state=ProcessState.TIMED_OUT,
                exit_code=None,
                stdout="ws://127.0.0.1:9222/devtools/browser/private-id",
                stderr="private child output",
            )

    monkeypatch.setattr(attach, "ProcessRunner", FakeRunner)

    with pytest.raises(BrowserAttachError, match="whole-process timeout") as caught:
        BrowserCdpAttacher().inspect("http://127.0.0.1:9222")
    assert "private-id" not in str(caught.value)
    assert "private child output" not in str(caught.value)


@pytest.mark.parametrize(
    "stdout_truncated, stderr_truncated",
    [(True, False), (False, True)],
)
def test_attach_rejects_truncated_helper_output(
    monkeypatch: pytest.MonkeyPatch,
    stdout_truncated: bool,
    stderr_truncated: bool,
) -> None:
    class FakeRunner:
        def run(self, argv, *, cwd=None, env_overrides=None, limits=None):
            return _process_result(
                stdout=json.dumps(_evidence().as_dict()),
                stdout_truncated=stdout_truncated,
                stderr_truncated=stderr_truncated,
            )

    monkeypatch.setattr(attach, "ProcessRunner", FakeRunner)

    with pytest.raises(BrowserAttachError, match="output exceeded"):
        BrowserCdpAttacher().inspect("http://127.0.0.1:9222")


def test_attach_rejects_wrong_helper_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = _evidence().as_dict()
    payload["endpoint"] = "http://127.0.0.1:9333"

    class FakeRunner:
        def run(self, argv, *, cwd=None, env_overrides=None, limits=None):
            return _process_result(stdout=json.dumps(payload))

    monkeypatch.setattr(attach, "ProcessRunner", FakeRunner)

    with pytest.raises(BrowserAttachError, match="wrong endpoint"):
        BrowserCdpAttacher().inspect("http://127.0.0.1:9222")


def test_attach_rejects_unsanitized_target_url(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = _evidence().as_dict()
    payload["targets"][0]["url"] = "https://example.test/path?token=secret#fragment"

    class FakeRunner:
        def run(self, argv, *, cwd=None, env_overrides=None, limits=None):
            return _process_result(stdout=json.dumps(payload))

    monkeypatch.setattr(attach, "ProcessRunner", FakeRunner)

    with pytest.raises(BrowserAttachError, match="unsanitized target URL"):
        BrowserCdpAttacher().inspect("http://127.0.0.1:9222")


def test_failed_helper_redacts_websocket_handle_and_stderr(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = {"error": "connect failed at ws://127.0.0.1:9222/devtools/browser/private-id"}

    class FakeRunner:
        def run(self, argv, *, cwd=None, env_overrides=None, limits=None):
            return _process_result(
                stdout=json.dumps(payload),
                stderr="do not expose stderr",
                exit_code=1,
            )

    monkeypatch.setattr(attach, "ProcessRunner", FakeRunner)

    with pytest.raises(BrowserAttachError) as caught:
        BrowserCdpAttacher().inspect("http://127.0.0.1:9222")
    message = str(caught.value)
    assert "private-id" not in message
    assert "do not expose" not in message
    assert "<redacted-cdp-url>" in message


def test_attach_model_emits_no_websocket_or_page_content_fields() -> None:
    payload = _evidence().as_dict()

    assert payload == {
        "endpoint": "http://127.0.0.1:9222",
        "browser_version": "153.0.8010.12",
        "context_count": 1,
        "targets": [
            {
                "id": "page-1",
                "type": "page",
                "title": "Example",
                "url": "https://example.test/path",
                "attached": False,
            }
        ],
    }


@pytest.mark.parametrize(
    ("stdout", "message"),
    [("not-json", "invalid JSON"), ("[]", "non-object JSON")],
)
def test_attach_rejects_invalid_helper_json_shape(
    monkeypatch: pytest.MonkeyPatch, stdout: str, message: str
) -> None:
    class FakeRunner:
        def run(self, argv, *, cwd=None, env_overrides=None, limits=None):
            return _process_result(stdout=stdout)

    monkeypatch.setattr(attach, "ProcessRunner", FakeRunner)

    with pytest.raises(BrowserAttachError, match=message):
        BrowserCdpAttacher().inspect("http://127.0.0.1:9222")


def test_failed_helper_without_structured_error_is_generic(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeRunner:
        def run(self, argv, *, cwd=None, env_overrides=None, limits=None):
            return _process_result(stdout="{}", stderr="private detail", exit_code=2)

    monkeypatch.setattr(attach, "ProcessRunner", FakeRunner)

    with pytest.raises(BrowserAttachError, match="helper failed") as caught:
        BrowserCdpAttacher().inspect("http://127.0.0.1:9222")
    assert "private detail" not in str(caught.value)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("browser_version", "", "invalid browser version"),
        ("browser_version", "x" * 129, "oversized browser version"),
        ("context_count", True, "invalid context count"),
        ("context_count", -1, "invalid context count"),
        ("targets", {}, "invalid target list"),
        ("targets", [None] * 513, "invalid target list"),
    ],
)
def test_attach_rejects_invalid_top_level_helper_payload(
    field: str, value: object, message: str
) -> None:
    payload = _evidence().as_dict()
    payload[field] = value

    with pytest.raises(BrowserAttachError, match=message):
        attach._inspection_from_payload(payload, expected_endpoint="http://127.0.0.1:9222")


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        (None, "invalid target"),
        (
            {
                "id": "",
                "type": "page",
                "title": "x",
                "url": "https://example.test/",
                "attached": False,
            },
            "invalid target id",
        ),
        (
            {
                "id": "x",
                "type": "",
                "title": "x",
                "url": "https://example.test/",
                "attached": False,
            },
            "invalid target type",
        ),
        (
            {
                "id": "x",
                "type": "page",
                "title": "x" * 513,
                "url": "https://example.test/",
                "attached": False,
            },
            "invalid target title",
        ),
        (
            {
                "id": "x",
                "type": "page",
                "title": "x",
                "url": "https://example.test/",
                "attached": "no",
            },
            "invalid attached state",
        ),
    ],
)
def test_attach_rejects_invalid_target_payload(raw: object, message: str) -> None:
    with pytest.raises(BrowserAttachError, match=message):
        attach._target_from_payload(raw)
