from __future__ import annotations

import pytest

from local_agent.host_ops.capabilities.local.browser import (
    BrowserCdpContentScriptRecoverer,
    BrowserContentScriptRecoveryError,
    BrowserExtensionScriptEvidence,
    BrowserReadinessError,
    BrowserReadinessInspection,
    BrowserReloadError,
    BrowserReloadResult,
    BrowserSelectorCount,
)

ENDPOINT = "http://127.0.0.1:9222"
TARGET = "page-1"
URL = "https://example.test/path"
DIGEST = "a" * 64


def _readiness(
    diagnosis: str,
    *,
    url: str = URL,
    content_state: str = "ready",
    dom_ready: bool = True,
    worker_state: str = "running",
) -> BrowserReadinessInspection:
    script_status = {
        "ready": "matched",
        "missing": "missing",
        "stale": "stale",
        "ambiguous": "matched",
    }[content_state]
    return BrowserReadinessInspection(
        endpoint=ENDPOINT,
        target_id=TARGET,
        target_type="page",
        title="Example",
        url=url,
        selectors=(BrowserSelectorCount("#ready", 1 if dom_ready else 0),),
        extension_scripts=(BrowserExtensionScriptEvidence("content.js", script_status),),
        dom_ready=dom_ready,
        content_script_state=content_state,
        worker_state=worker_state,
        diagnosis=diagnosis,
    )


def _reload(*, after_url: str = URL) -> BrowserReloadResult:
    return BrowserReloadResult(
        endpoint=ENDPOINT,
        target_id=TARGET,
        target_type="page",
        expected_url=URL,
        before_url=URL,
        before_title="Before",
        after_url=after_url,
        after_title="After",
    )


class FakeInspector:
    def __init__(self, results=None, error: Exception | None = None) -> None:
        self.results = list(results or [])
        self.error = error
        self.calls = []

    def inspect(self, endpoint, target_id, selectors, fingerprints, *, timeout_seconds):
        self.calls.append((endpoint, target_id, selectors, fingerprints, timeout_seconds))
        if self.error is not None:
            raise self.error
        if not self.results:
            raise AssertionError("unexpected readiness inspection")
        return self.results.pop(0)


class FakeReloader:
    def __init__(self, result=None, error: Exception | None = None) -> None:
        self.result = result or _reload()
        self.error = error
        self.calls = []

    def reload(self, endpoint, target_id, expected_url, *, timeout_seconds):
        self.calls.append((endpoint, target_id, expected_url, timeout_seconds))
        if self.error is not None:
            raise self.error
        return self.result


def test_ready_normalizes_inputs_and_does_not_reload() -> None:
    inspector = FakeInspector([_readiness("ready")])
    reloader = FakeReloader()
    recoverer = BrowserCdpContentScriptRecoverer(inspector, reloader, clock=lambda: 100.0)

    result = recoverer.recover(
        " http://127.0.0.1:9222/ ",
        " page-1 ",
        " https://example.test/path ",
        [" #ready ", "#ready"],
        [f" content.js={DIGEST.upper()} "],
        timeout_seconds=30,
    )

    assert result.action == "none"
    assert result.outcome == "not_needed"
    assert result.reload is None
    assert result.after is None
    assert reloader.calls == []
    assert inspector.calls == [(ENDPOINT, TARGET, ("#ready",), (f"content.js={DIGEST}",), 30.0)]


@pytest.mark.parametrize(
    ("before", "expected_outcome"),
    [
        (_readiness("ready"), "not_needed"),
        (
            _readiness("worker_inactive", content_state="ready", worker_state="inactive"),
            "not_needed",
        ),
        (
            _readiness("dom_not_ready", content_state="missing", dom_ready=False),
            "not_applicable",
        ),
        (
            _readiness("extension_ambiguous", content_state="ambiguous"),
            "not_applicable",
        ),
    ],
)
def test_non_healable_diagnoses_never_mutate(before, expected_outcome) -> None:
    inspector = FakeInspector([before])
    reloader = FakeReloader()
    result = BrowserCdpContentScriptRecoverer(inspector, reloader, clock=lambda: 0.0).recover(
        ENDPOINT, TARGET, URL, ["#ready"], [f"content.js={DIGEST}"]
    )

    assert result.action == "none"
    assert result.outcome == expected_outcome
    assert reloader.calls == []


def test_expected_url_mismatch_fails_before_mutation() -> None:
    inspector = FakeInspector([_readiness("content_script_stale", url="https://other.test/")])
    reloader = FakeReloader()
    with pytest.raises(BrowserContentScriptRecoveryError, match="expected URL guard"):
        BrowserCdpContentScriptRecoverer(inspector, reloader, clock=lambda: 0.0).recover(
            ENDPOINT, TARGET, URL, ["#ready"], [f"content.js={DIGEST}"]
        )
    assert reloader.calls == []


@pytest.mark.parametrize("before_state", ["missing", "stale"])
def test_missing_or_stale_reloads_once_and_recovers(before_state: str) -> None:
    before = _readiness(f"content_script_{before_state}", content_state=before_state)
    after = _readiness("worker_inactive", content_state="ready", worker_state="inactive")
    inspector = FakeInspector([before, after])
    reloader = FakeReloader(_reload())
    result = BrowserCdpContentScriptRecoverer(inspector, reloader, clock=lambda: 10.0).recover(
        ENDPOINT,
        TARGET,
        URL,
        ["#ready"],
        [f"content.js={DIGEST}"],
        timeout_seconds=30,
    )

    assert result.action == "reload"
    assert result.outcome == "recovered"
    assert result.reload == _reload()
    assert result.after == after
    assert len(reloader.calls) == 1
    assert len(inspector.calls) == 2


def test_reload_can_finish_without_recovery() -> None:
    before = _readiness("content_script_stale", content_state="stale")
    after = _readiness("content_script_stale", content_state="stale")
    result = BrowserCdpContentScriptRecoverer(
        FakeInspector([before, after]), FakeReloader(), clock=lambda: 0.0
    ).recover(ENDPOINT, TARGET, URL, ["#ready"], [f"content.js={DIGEST}"])

    assert result.action == "reload"
    assert result.outcome == "not_recovered"
    assert result.after == after


def test_reload_target_change_stops_before_post_check() -> None:
    inspector = FakeInspector([_readiness("content_script_missing", content_state="missing")])
    reloader = FakeReloader(_reload(after_url="https://example.test/login"))
    result = BrowserCdpContentScriptRecoverer(inspector, reloader, clock=lambda: 0.0).recover(
        ENDPOINT, TARGET, URL, ["#ready"], [f"content.js={DIGEST}"]
    )

    assert result.action == "reload"
    assert result.outcome == "target_changed"
    assert result.after is None
    assert len(inspector.calls) == 1
    assert len(reloader.calls) == 1


def test_post_check_url_change_fails_closed() -> None:
    before = _readiness("content_script_stale", content_state="stale")
    after = _readiness("ready", url="https://example.test/other")
    with pytest.raises(BrowserContentScriptRecoveryError, match="post-check target URL changed"):
        BrowserCdpContentScriptRecoverer(
            FakeInspector([before, after]), FakeReloader(), clock=lambda: 0.0
        ).recover(ENDPOINT, TARGET, URL, ["#ready"], [f"content.js={DIGEST}"])


def test_readiness_and_reload_errors_are_wrapped() -> None:
    with pytest.raises(BrowserContentScriptRecoveryError, match="readiness failed"):
        BrowserCdpContentScriptRecoverer(
            FakeInspector(error=BrowserReadinessError("readiness failed")),
            FakeReloader(),
            clock=lambda: 0.0,
        ).recover(ENDPOINT, TARGET, URL, ["#ready"], [f"content.js={DIGEST}"])

    with pytest.raises(BrowserContentScriptRecoveryError, match="reload failed"):
        BrowserCdpContentScriptRecoverer(
            FakeInspector([_readiness("content_script_stale", content_state="stale")]),
            FakeReloader(error=BrowserReloadError("reload failed")),
            clock=lambda: 0.0,
        ).recover(ENDPOINT, TARGET, URL, ["#ready"], [f"content.js={DIGEST}"])


def test_whole_operation_deadline_blocks_reload_when_budget_is_exhausted() -> None:
    times = iter([100.0, 100.0, 102.5])
    inspector = FakeInspector([_readiness("content_script_stale", content_state="stale")])
    reloader = FakeReloader()
    with pytest.raises(BrowserContentScriptRecoveryError, match="exceeded 3s"):
        BrowserCdpContentScriptRecoverer(inspector, reloader, clock=lambda: next(times)).recover(
            ENDPOINT,
            TARGET,
            URL,
            ["#ready"],
            [f"content.js={DIGEST}"],
            timeout_seconds=3,
        )
    assert reloader.calls == []


def test_rejects_too_short_or_long_timeout() -> None:
    recoverer = BrowserCdpContentScriptRecoverer(
        FakeInspector([_readiness("ready")]), FakeReloader(), clock=lambda: 0.0
    )
    for timeout in (2.9, 121):
        with pytest.raises(ValueError, match="timeout_seconds"):
            recoverer.recover(
                ENDPOINT,
                TARGET,
                URL,
                ["#ready"],
                [f"content.js={DIGEST}"],
                timeout_seconds=timeout,
            )
