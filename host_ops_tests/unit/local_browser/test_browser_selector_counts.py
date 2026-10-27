from __future__ import annotations

import importlib
import json

import pytest

from local_agent.host_ops.capabilities.local.browser import (
    BrowserCdpSelectorCounter,
    BrowserSelectorCount,
    BrowserSelectorCountError,
    BrowserSelectorInspection,
)
from local_agent.host_ops.core.execution import ProcessResult, ProcessState

selector_counts = importlib.import_module("local_agent.host_ops.capabilities.local.browser.selector_counts")


def _evidence() -> BrowserSelectorInspection:
    return BrowserSelectorInspection(
        endpoint="http://127.0.0.1:9222",
        target_id="page-1",
        target_type="page",
        title="Example",
        url="https://example.test/path",
        selectors=(
            BrowserSelectorCount("#prompt-textarea", 1),
            BrowserSelectorCount("#composer-submit-button", 0),
        ),
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


def test_counter_normalizes_inputs_and_deduplicates_selectors() -> None:
    calls = []

    def backend(endpoint, target_id, selectors, timeout):
        calls.append((endpoint, target_id, selectors, timeout))
        return _evidence()

    result = BrowserCdpSelectorCounter(backend).count(
        " http://127.0.0.1:9222/ ",
        " page-1 ",
        [" #prompt-textarea ", "#prompt-textarea", "#composer-submit-button"],
        timeout_seconds=4.5,
    )
    assert result == _evidence()
    assert calls == [
        (
            "http://127.0.0.1:9222",
            "page-1",
            ("#prompt-textarea", "#composer-submit-button"),
            4.5,
        )
    ]


@pytest.mark.parametrize(
    "selectors",
    [[], [""], ["x" * 257], ["a\nb"], ["x"] * 17, [object()]],
)
def test_counter_rejects_invalid_selectors(selectors) -> None:
    with pytest.raises(ValueError, match="selector"):
        BrowserCdpSelectorCounter(lambda *_args: _evidence()).count(
            "http://127.0.0.1:9222", "page-1", selectors
        )


def test_counter_rejects_invalid_timeout() -> None:
    with pytest.raises(ValueError, match="timeout_seconds"):
        BrowserCdpSelectorCounter(lambda *_args: _evidence()).count(
            "http://127.0.0.1:9222", "page-1", ["#x"], timeout_seconds=0
        )


def test_helper_uses_environment_and_parent_revalidates(monkeypatch) -> None:
    calls = []

    class FakeRunner:
        def run(self, argv, *, cwd=None, env_overrides=None, limits=None):
            calls.append((list(argv), dict(env_overrides or {}), limits))
            return _result(stdout=json.dumps(_evidence().as_dict()))

    monkeypatch.setattr(selector_counts, "ProcessRunner", FakeRunner)
    result = BrowserCdpSelectorCounter().count(
        "http://127.0.0.1:9222",
        "page-1",
        ["#prompt-textarea", "#composer-submit-button"],
        timeout_seconds=7,
    )
    assert result == _evidence()
    argv, env, limits = calls[0]
    assert argv[2] == (
        "local_agent.host_ops.capabilities.local.browser.playwright_selector_counts"
    )
    assert "#prompt-textarea" not in argv
    assert json.loads(env[selector_counts._SELECTORS_ENV]) == [
        "#prompt-textarea",
        "#composer-submit-button",
    ]
    assert limits.timeout_seconds == 7

    payload = _evidence().as_dict()
    payload["selectors"][0]["selector"] = "other"
    with pytest.raises(BrowserSelectorCountError, match="wrong selector"):
        selector_counts._inspection_from_payload(
            payload,
            endpoint="http://127.0.0.1:9222",
            target_id="page-1",
            selectors=("#prompt-textarea", "#composer-submit-button"),
        )


def test_helper_process_failures_fail_closed(monkeypatch) -> None:
    class FakeRunner:
        result = _result(state=ProcessState.TIMED_OUT, exit_code=None)

        def run(self, *args, **kwargs):
            return self.result

    monkeypatch.setattr(selector_counts, "ProcessRunner", FakeRunner)
    with pytest.raises(BrowserSelectorCountError, match="exceeded 10s"):
        BrowserCdpSelectorCounter().count("http://127.0.0.1:9222", "page-1", ["#x"])

    FakeRunner.result = _result(stdout_truncated=True)
    with pytest.raises(BrowserSelectorCountError, match="output exceeded"):
        BrowserCdpSelectorCounter().count("http://127.0.0.1:9222", "page-1", ["#x"])

    FakeRunner.result = _result(
        exit_code=7,
        stdout=json.dumps({"error": "failed at https://example.test/path?token=value"}),
    )
    with pytest.raises(BrowserSelectorCountError) as exc_info:
        BrowserCdpSelectorCounter().count("http://127.0.0.1:9222", "page-1", ["#x"])
    assert "token=value" not in str(exc_info.value)

    FakeRunner.result = _result(exit_code=9, stdout="{}")
    with pytest.raises(BrowserSelectorCountError, match=r"helper failed: completed exit=9"):
        BrowserCdpSelectorCounter().count("http://127.0.0.1:9222", "page-1", ["#x"])


def test_parent_rejects_invalid_helper_json() -> None:
    with pytest.raises(BrowserSelectorCountError, match="invalid JSON"):
        selector_counts._parse_helper_json("not-json")
    with pytest.raises(BrowserSelectorCountError, match="non-object JSON"):
        selector_counts._parse_helper_json("[]")


def test_parent_rejects_invalid_payload_identity_and_metadata() -> None:
    selectors = ("#prompt-textarea", "#composer-submit-button")
    cases = [
        ("endpoint", "http://127.0.0.1:9333", "wrong endpoint"),
        ("target_id", "other", "wrong target id"),
        ("target_type", "worker", "non-page target"),
        ("title", 7, "invalid title"),
        ("url", "https://example.test/path?token=value", "unsanitized url"),
        ("selectors", [], "invalid selector list"),
    ]
    for key, value, message in cases:
        payload = _evidence().as_dict()
        payload[key] = value
        with pytest.raises(BrowserSelectorCountError, match=message):
            selector_counts._inspection_from_payload(
                payload,
                endpoint="http://127.0.0.1:9222",
                target_id="page-1",
                selectors=selectors,
            )


def test_parent_rejects_invalid_match_count() -> None:
    payload = _evidence().as_dict()
    payload["selectors"][0]["match_count"] = True
    with pytest.raises(BrowserSelectorCountError, match="invalid match count"):
        selector_counts._inspection_from_payload(
            payload,
            endpoint="http://127.0.0.1:9222",
            target_id="page-1",
            selectors=("#prompt-textarea", "#composer-submit-button"),
        )
