from __future__ import annotations

import importlib
import json

import pytest

from local_agent.host_ops.capabilities.local.browser import (
    BrowserCdpReloader,
    BrowserReloadError,
    BrowserReloadResult,
)
from local_agent.host_ops.core.execution import ProcessResult, ProcessState

reload = importlib.import_module("local_agent.host_ops.capabilities.local.browser.reload")


def _evidence() -> BrowserReloadResult:
    return BrowserReloadResult(
        endpoint="http://127.0.0.1:9222",
        target_id="page-1",
        target_type="page",
        expected_url="https://example.test/path",
        before_url="https://example.test/path",
        before_title="Before",
        after_url="https://example.test/path",
        after_title="After",
    )


def _result(**kwargs) -> ProcessResult:
    defaults = dict(
        state=ProcessState.COMPLETED,
        exit_code=0,
        stdout="",
        stderr="",
        duration_seconds=0.1,
    )
    defaults.update(kwargs)
    return ProcessResult(**defaults)


def test_reload_validates_and_normalizes_before_backend() -> None:
    calls = []

    def backend(endpoint: str, target_id: str, expected_url: str, timeout_seconds: float):
        calls.append((endpoint, target_id, expected_url, timeout_seconds))
        return _evidence()

    result = BrowserCdpReloader(backend).reload(
        " http://127.0.0.1:9222/ ",
        " page-1 ",
        " https://example.test/path ",
        timeout_seconds=4.5,
    )
    assert result == _evidence()
    assert calls == [("http://127.0.0.1:9222", "page-1", "https://example.test/path", 4.5)]


@pytest.mark.parametrize(
    "expected_url",
    [
        "",
        "about:blank",
        "https://example.test/path?secret=x",
        "https://example.test/path#fragment",
        "https://user:pass@example.test/path",
        "https://EXAMPLE.test/path",
    ],
)
def test_reload_rejects_non_sanitized_or_non_http_expected_url(expected_url: str) -> None:
    with pytest.raises(ValueError, match="expected_url"):
        BrowserCdpReloader(lambda *_args: _evidence()).reload(
            "http://127.0.0.1:9222", "page-1", expected_url
        )


@pytest.mark.parametrize("timeout_seconds", [0, 0.5, 120.1])
def test_reload_rejects_invalid_timeout(timeout_seconds: float) -> None:
    with pytest.raises(ValueError, match="timeout_seconds"):
        BrowserCdpReloader(lambda *_args: _evidence()).reload(
            "http://127.0.0.1:9222",
            "page-1",
            "https://example.test/path",
            timeout_seconds=timeout_seconds,
        )


def test_reload_helper_keeps_sensitive_inputs_out_of_argv(monkeypatch) -> None:
    calls = []

    class FakeRunner:
        def run(self, argv, *, cwd=None, env_overrides=None, limits=None):
            calls.append((list(argv), dict(env_overrides or {}), limits))
            return _result(stdout=json.dumps(_evidence().as_dict()))

    monkeypatch.setattr(reload, "ProcessRunner", FakeRunner)
    result = BrowserCdpReloader().reload(
        "http://127.0.0.1:9222",
        "page-1",
        "https://example.test/path",
        timeout_seconds=7,
    )
    assert result == _evidence()
    argv, env, limits = calls[0]
    assert argv[2] == (
        "local_agent.host_ops.capabilities.local.browser.playwright_reload"
    )
    assert "http://127.0.0.1:9222" not in argv
    assert "page-1" not in argv
    assert "https://example.test/path" not in argv
    assert env["HOST_OPS_BROWSER_ATTACH_ENDPOINT"] == "http://127.0.0.1:9222"
    assert env["HOST_OPS_BROWSER_ATTACH_TARGET_ID"] == "page-1"
    assert env[reload._RELOAD_EXPECTED_URL_ENV] == "https://example.test/path"
    assert limits.timeout_seconds == 7


def test_reload_timeout_truncation_and_invalid_json_fail_closed(monkeypatch) -> None:
    class TimeoutRunner:
        def run(self, *args, **kwargs):
            return _result(state=ProcessState.TIMED_OUT, exit_code=None, stdout="private")

    monkeypatch.setattr(reload, "ProcessRunner", TimeoutRunner)
    with pytest.raises(BrowserReloadError, match="whole-process timeout"):
        BrowserCdpReloader().reload("http://127.0.0.1:9222", "page-1", "https://example.test/path")

    class TruncatedRunner:
        def run(self, *args, **kwargs):
            return _result(stdout=json.dumps(_evidence().as_dict()), stdout_truncated=True)

    monkeypatch.setattr(reload, "ProcessRunner", TruncatedRunner)
    with pytest.raises(BrowserReloadError, match="output exceeded"):
        BrowserCdpReloader().reload("http://127.0.0.1:9222", "page-1", "https://example.test/path")

    with pytest.raises(BrowserReloadError, match="invalid JSON"):
        reload._parse_helper_json("not-json")
    with pytest.raises(BrowserReloadError, match="non-object JSON"):
        reload._parse_helper_json("[]")


def test_reload_strictly_revalidates_identity_guard_and_shape() -> None:
    payload = _evidence().as_dict()
    assert (
        reload._result_from_payload(
            payload,
            endpoint="http://127.0.0.1:9222",
            target_id="page-1",
            expected_url="https://example.test/path",
        )
        == _evidence()
    )

    mutated = dict(payload, private_text="must not pass")
    with pytest.raises(BrowserReloadError, match="unexpected payload shape"):
        reload._result_from_payload(
            mutated,
            endpoint="http://127.0.0.1:9222",
            target_id="page-1",
            expected_url="https://example.test/path",
        )

    for key, value, message in (
        ("endpoint", "http://127.0.0.1:9333", "wrong endpoint"),
        ("target_id", "other", "wrong target id"),
        ("target_type", "worker", "non-page target"),
        ("expected_url", "https://other.test/", "wrong expected URL"),
        ("before_url", "https://other.test/", "expected URL guard"),
        ("after_url", "about:", "invalid after_url"),
        ("before_title", "x" * 513, "invalid before_title"),
        ("after_title", object(), "invalid after_title"),
    ):
        mutated = dict(payload)
        mutated[key] = value
        with pytest.raises(BrowserReloadError, match=message):
            reload._result_from_payload(
                mutated,
                endpoint="http://127.0.0.1:9222",
                target_id="page-1",
                expected_url="https://example.test/path",
            )


def test_reload_helper_failure_is_redacted(monkeypatch) -> None:
    class FailureRunner:
        def run(self, *args, **kwargs):
            return _result(
                exit_code=7,
                stdout=json.dumps(
                    {"error": "reload failed at https://example.test/path?token=secret"}
                ),
            )

    monkeypatch.setattr(reload, "ProcessRunner", FailureRunner)
    with pytest.raises(BrowserReloadError) as exc_info:
        BrowserCdpReloader().reload("http://127.0.0.1:9222", "page-1", "https://example.test/path")
    assert "secret" not in str(exc_info.value)
    assert "<redacted-url>" in str(exc_info.value)


def test_reload_result_contains_only_bounded_metadata() -> None:
    payload = _evidence().as_dict()
    assert set(payload) == reload._EXPECTED_PAYLOAD_KEYS
    assert "html" not in payload
    assert "cookies" not in payload
    assert "text" not in payload
