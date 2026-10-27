from __future__ import annotations

import importlib
import json

import pytest

from local_agent.host_ops.capabilities.local.browser import (
    BrowserCdpReadinessInspector,
    BrowserExtensionScriptEvidence,
    BrowserReadinessError,
    BrowserReadinessInspection,
    BrowserSelectorCount,
)
from local_agent.host_ops.core.execution import ProcessResult, ProcessState

readiness = importlib.import_module("local_agent.host_ops.capabilities.local.browser.readiness")

DIGEST_A = "a" * 64
DIGEST_B = "b" * 64


def _evidence() -> BrowserReadinessInspection:
    return BrowserReadinessInspection(
        endpoint="http://127.0.0.1:9222",
        target_id="page-1",
        target_type="page",
        title="Example",
        url="https://example.test/path",
        selectors=(BrowserSelectorCount("#prompt-textarea", 1),),
        extension_scripts=(BrowserExtensionScriptEvidence("content.js", "matched"),),
        dom_ready=True,
        content_script_state="ready",
        worker_state="running",
        diagnosis="ready",
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


def test_inspector_normalizes_inputs_and_backend_arguments() -> None:
    calls = []

    def backend(endpoint, target_id, selectors, fingerprints, timeout):
        calls.append((endpoint, target_id, selectors, fingerprints, timeout))
        return _evidence()

    result = BrowserCdpReadinessInspector(backend).inspect(
        " http://127.0.0.1:9222/ ",
        " page-1 ",
        [" #prompt-textarea ", "#prompt-textarea"],
        [f" content.js={DIGEST_A.upper()} "],
        timeout_seconds=4.5,
    )
    assert result == _evidence()
    assert calls == [
        (
            "http://127.0.0.1:9222",
            "page-1",
            ("#prompt-textarea",),
            (("content.js", DIGEST_A),),
            4.5,
        )
    ]


@pytest.mark.parametrize(
    "fingerprints",
    [
        [],
        ["content.js"],
        ["../content.js=" + DIGEST_A],
        ["dir/content.js=" + DIGEST_A],
        ["content.js=short"],
        ["content.js=" + "z" * 64],
        ["content.js=" + DIGEST_A, "content.js=" + DIGEST_B],
        [f"file{index}.js={DIGEST_A}" for index in range(17)],
        [object()],
    ],
)
def test_inspector_rejects_invalid_script_fingerprints(fingerprints) -> None:
    with pytest.raises(ValueError, match=r"script fingerprint|script_fingerprints"):
        BrowserCdpReadinessInspector(lambda *_args: _evidence()).inspect(
            "http://127.0.0.1:9222", "page-1", ["#x"], fingerprints
        )


def test_inspector_rejects_invalid_timeout() -> None:
    with pytest.raises(ValueError, match="timeout_seconds"):
        BrowserCdpReadinessInspector(lambda *_args: _evidence()).inspect(
            "http://127.0.0.1:9222",
            "page-1",
            ["#x"],
            [f"content.js={DIGEST_A}"],
            timeout_seconds=0,
        )


def test_helper_inputs_are_environment_only_and_parent_revalidates(monkeypatch) -> None:
    calls = []

    class FakeRunner:
        def run(self, argv, *, cwd=None, env_overrides=None, limits=None):
            calls.append((list(argv), dict(env_overrides or {}), limits))
            return _result(stdout=json.dumps(_evidence().as_dict()))

    monkeypatch.setattr(readiness, "ProcessRunner", FakeRunner)
    result = BrowserCdpReadinessInspector().inspect(
        "http://127.0.0.1:9222",
        "page-1",
        ["#prompt-textarea"],
        [f"content.js={DIGEST_A}"],
        timeout_seconds=7,
    )
    assert result == _evidence()
    argv, env, limits = calls[0]
    assert argv[2] == (
        "local_agent.host_ops.capabilities.local.browser.playwright_readiness"
    )
    assert "#prompt-textarea" not in argv
    assert DIGEST_A not in argv
    assert json.loads(env["HOST_OPS_BROWSER_SELECTOR_QUERIES"]) == ["#prompt-textarea"]
    assert json.loads(env[readiness._SCRIPT_FINGERPRINTS_ENV]) == [["content.js", DIGEST_A]]
    assert limits.timeout_seconds == 7


def test_helper_process_failures_fail_closed(monkeypatch) -> None:
    class FakeRunner:
        result = _result(state=ProcessState.TIMED_OUT, exit_code=None)

        def run(self, *args, **kwargs):
            return self.result

    monkeypatch.setattr(readiness, "ProcessRunner", FakeRunner)

    def inspect() -> BrowserReadinessInspection:
        return BrowserCdpReadinessInspector().inspect(
            "http://127.0.0.1:9222",
            "page-1",
            ["#x"],
            [f"content.js={DIGEST_A}"],
        )

    with pytest.raises(BrowserReadinessError, match="exceeded 10s"):
        inspect()

    FakeRunner.result = _result(stdout_truncated=True)
    with pytest.raises(BrowserReadinessError, match="output exceeded"):
        inspect()

    FakeRunner.result = _result(
        exit_code=7,
        stdout=json.dumps({"error": "failed at https://example.test/path?token=private"}),
    )
    with pytest.raises(BrowserReadinessError) as exc_info:
        inspect()
    assert "token=private" not in str(exc_info.value)

    FakeRunner.result = _result(exit_code=9, stdout="{}")
    with pytest.raises(BrowserReadinessError, match=r"helper failed: completed exit=9"):
        inspect()


def test_parent_rejects_invalid_json() -> None:
    with pytest.raises(BrowserReadinessError, match="invalid JSON"):
        readiness._parse_helper_json("not-json")
    with pytest.raises(BrowserReadinessError, match="non-object JSON"):
        readiness._parse_helper_json("[]")


def test_parent_rejects_invalid_identity_metadata_and_states() -> None:
    selectors = ("#prompt-textarea",)
    fingerprints = (("content.js", DIGEST_A),)
    cases = [
        ("endpoint", "http://127.0.0.1:9333", "wrong endpoint"),
        ("target_id", "other", "wrong page target"),
        ("target_type", "worker", "wrong page target"),
        ("title", 7, "invalid title"),
        ("url", "https://example.test/path?token=private", "unsanitized url"),
        ("selectors", [], "invalid selector list"),
        ("extension_scripts", [], "invalid extension script list"),
        ("content_script_state", "broken", "invalid content-script state"),
        ("worker_state", "broken", "invalid worker state"),
        ("diagnosis", "broken", "invalid diagnosis"),
    ]
    for key, value, message in cases:
        payload = _evidence().as_dict()
        payload[key] = value
        with pytest.raises(BrowserReadinessError, match=message):
            readiness._inspection_from_payload(
                payload,
                endpoint="http://127.0.0.1:9222",
                target_id="page-1",
                selectors=selectors,
                script_fingerprints=fingerprints,
            )


def test_parent_rejects_invalid_script_status_and_dom_consistency() -> None:
    payload = _evidence().as_dict()
    payload["extension_scripts"][0]["status"] = "unknown"
    with pytest.raises(BrowserReadinessError, match="invalid script status"):
        readiness._inspection_from_payload(
            payload,
            endpoint="http://127.0.0.1:9222",
            target_id="page-1",
            selectors=("#prompt-textarea",),
            script_fingerprints=(("content.js", DIGEST_A),),
        )

    payload = _evidence().as_dict()
    payload["selectors"][0]["match_count"] = 0
    with pytest.raises(BrowserReadinessError, match="inconsistent DOM readiness"):
        readiness._inspection_from_payload(
            payload,
            endpoint="http://127.0.0.1:9222",
            target_id="page-1",
            selectors=("#prompt-textarea",),
            script_fingerprints=(("content.js", DIGEST_A),),
        )
