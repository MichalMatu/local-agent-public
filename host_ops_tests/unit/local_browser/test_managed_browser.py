from __future__ import annotations

import importlib
import json

import pytest

from local_agent.host_ops.capabilities.local.browser import (
    ManagedBrowserError,
    ManagedBrowserProbe,
    ManagedBrowserProber,
)
from local_agent.host_ops.core.execution import ProcessResult, ProcessState

managed = importlib.import_module("local_agent.host_ops.capabilities.local.browser.managed")
playwright_probe = importlib.import_module("local_agent.host_ops.capabilities.local.browser.playwright_probe")


def _evidence(*, engine: str = "chromium") -> ManagedBrowserProbe:
    return ManagedBrowserProbe(
        engine=engine,
        requested_url="https://example.test/path",
        final_url="https://example.test/final",
        title="Example",
        status_code=200,
        console_error_count=1,
        page_error_count=2,
        request_failure_count=3,
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


def test_probe_validates_target_engine_and_timeout_before_backend() -> None:
    calls: list[tuple[str, str, float]] = []

    def backend(url: str, engine: str, timeout_seconds: float) -> ManagedBrowserProbe:
        calls.append((url, engine, timeout_seconds))
        return _evidence(engine=engine)

    result = ManagedBrowserProber(backend).probe(
        "  https://example.test/path?token=secret#fragment  ",
        engine=" CHROMIUM ",
        timeout_seconds=4.5,
    )

    assert result.engine == "chromium"
    assert calls == [("https://example.test/path?token=secret#fragment", "chromium", 4.5)]


@pytest.mark.parametrize(
    "url, message",
    [
        ("ftp://example.test/path", "http or https"),
        ("https://user:secret@example.test/path", "must not contain credentials"),
        ("https:///missing-host", "must include a hostname"),
        ("https://example.test/has space", "must not contain whitespace"),
    ],
)
def test_probe_rejects_unsafe_or_unsupported_urls(url: str, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        ManagedBrowserProber(lambda *_args: _evidence()).probe(url)


@pytest.mark.parametrize("timeout_seconds", [0.0, 0.5, -1.0, 120.1])
def test_probe_rejects_invalid_timeout(timeout_seconds: float) -> None:
    with pytest.raises(ValueError, match="timeout_seconds"):
        ManagedBrowserProber(lambda *_args: _evidence()).probe(
            "https://example.test/",
            timeout_seconds=timeout_seconds,
        )


def test_probe_rejects_unknown_engine() -> None:
    with pytest.raises(ValueError, match="chromium, firefox, webkit"):
        ManagedBrowserProber(lambda *_args: _evidence()).probe(
            "https://example.test/",
            engine="chrome",
        )


def test_error_redaction_sanitizes_initial_and_redirect_urls() -> None:
    target = "https://example.test/path?token=initial#fragment"
    detail = (
        "Page.goto failed at https://example.test/path?token=initial#fragment "
        "after redirect to https://user:password@redirect.test:8443/final?session=super-secret#state"
    )

    redacted = managed._redact_probe_error(detail, target)

    assert "initial" not in redacted
    assert "password" not in redacted
    assert "super-secret" not in redacted
    assert "#fragment" not in redacted
    assert "#state" not in redacted
    assert "https://example.test/path" in redacted
    assert "https://redirect.test:8443/final" in redacted


def test_url_sanitizer_drops_credentials_query_and_fragment() -> None:
    assert (
        managed._sanitize_public_url(
            "https://user:password@example.test:8443/final?token=secret#fragment"
        )
        == "https://example.test:8443/final"
    )


def test_bounded_helper_uses_core_runner_and_keeps_raw_url_out_of_argv(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw_url = "https://example.test/path?token=secret#fragment"
    calls: list[tuple[list[str], dict[str, str], object]] = []
    stdout = json.dumps(_evidence().as_dict())

    class FakeRunner:
        def run(self, argv, *, cwd=None, env_overrides=None, limits=None):
            assert cwd is None
            calls.append((list(argv), dict(env_overrides or {}), limits))
            return _process_result(stdout=stdout)

    monkeypatch.setattr(managed, "ProcessRunner", FakeRunner)

    result = ManagedBrowserProber().probe(raw_url, timeout_seconds=7.0)

    assert result == _evidence()
    argv, environment, limits = calls[0]
    assert argv[2] == (
        "local_agent.host_ops.capabilities.local.browser.playwright_probe"
    )
    assert raw_url not in argv
    assert environment == {managed._PROBE_URL_ENV: raw_url}
    assert limits.timeout_seconds == 7.0
    assert limits.max_stdout_bytes == managed._MAX_HELPER_OUTPUT_BYTES
    assert limits.max_stderr_bytes == managed._MAX_HELPER_OUTPUT_BYTES


def test_bounded_helper_timeout_fails_without_exposing_child_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeRunner:
        def run(self, argv, *, cwd=None, env_overrides=None, limits=None):
            return _process_result(
                state=ProcessState.TIMED_OUT,
                exit_code=None,
                stdout="https://example.test/path?token=secret",
                stderr="private child output",
            )

    monkeypatch.setattr(managed, "ProcessRunner", FakeRunner)

    with pytest.raises(ManagedBrowserError, match="whole-process timeout") as caught:
        ManagedBrowserProber().probe("https://example.test/path?token=secret")
    assert "secret" not in str(caught.value)
    assert "private child output" not in str(caught.value)


@pytest.mark.parametrize(
    "stdout_truncated, stderr_truncated",
    [(True, False), (False, True)],
)
def test_bounded_helper_rejects_truncated_output(
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

    monkeypatch.setattr(managed, "ProcessRunner", FakeRunner)

    with pytest.raises(ManagedBrowserError, match="output exceeded"):
        ManagedBrowserProber().probe("https://example.test/")


def test_bounded_helper_rejects_unsanitized_result_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = _evidence().as_dict()
    payload["final_url"] = "https://example.test/final?token=secret"

    class FakeRunner:
        def run(self, argv, *, cwd=None, env_overrides=None, limits=None):
            return _process_result(stdout=json.dumps(payload))

    monkeypatch.setattr(managed, "ProcessRunner", FakeRunner)

    with pytest.raises(ManagedBrowserError, match="unsanitized final URL"):
        ManagedBrowserProber().probe("https://example.test/")


def test_failed_helper_uses_only_sanitized_structured_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw_url = "https://example.test/path?token=secret#fragment"
    payload = {
        "error": (
            "navigation failed at https://example.test/path?token=secret#fragment "
            "after https://user:password@redirect.test/final?session=hidden#state"
        )
    }

    class FakeRunner:
        def run(self, argv, *, cwd=None, env_overrides=None, limits=None):
            return _process_result(
                stdout=json.dumps(payload),
                stderr="never expose this stderr value",
                exit_code=1,
            )

    monkeypatch.setattr(managed, "ProcessRunner", FakeRunner)

    with pytest.raises(ManagedBrowserError) as caught:
        ManagedBrowserProber().probe(raw_url)
    message = str(caught.value)
    assert "secret" not in message
    assert "password" not in message
    assert "hidden" not in message
    assert "never expose" not in message
    assert "https://example.test/path" in message
    assert "https://redirect.test/final" in message


def test_optional_playwright_import_fails_with_actionable_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def missing(_name: str):
        raise ModuleNotFoundError("No module named 'playwright'", name="playwright")

    monkeypatch.setattr(playwright_probe.importlib, "import_module", missing)

    with pytest.raises(ManagedBrowserError, match=r"host-ops\[browser\]"):
        playwright_probe._load_playwright_sync_api()


def test_helper_operation_timeout_keeps_cleanup_reserve() -> None:
    assert playwright_probe._operation_timeout_ms(4.0) == 3000.0
    assert playwright_probe._operation_timeout_ms(1.0) == 750.0


def test_probe_model_emits_only_bounded_metadata_fields() -> None:
    payload = _evidence().as_dict()

    assert payload == {
        "engine": "chromium",
        "requested_url": "https://example.test/path",
        "final_url": "https://example.test/final",
        "title": "Example",
        "status_code": 200,
        "console_error_count": 1,
        "page_error_count": 2,
        "request_failure_count": 3,
    }
