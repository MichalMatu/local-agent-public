from __future__ import annotations

import importlib
import json
from types import SimpleNamespace

import pytest

from local_agent.host_ops.capabilities.local.browser import BrowserWorkerDiagnosticsError

playwright_workers = importlib.import_module(
    "local_agent.host_ops.capabilities.local.browser.playwright_worker_diagnostics"
)


class _Session:
    def __init__(
        self,
        *,
        target_response: object | None = None,
        registrations: object | None = None,
        versions: object | None = None,
        errors: tuple[object, ...] = (),
        fail_method: str | None = None,
    ) -> None:
        self.target_response = target_response
        self.registrations = registrations
        self.versions = versions
        self.errors = errors
        self.fail_method = fail_method
        self.handlers = {}
        self.calls: list[str] = []
        self.detached = False

    def on(self, event: str, handler) -> None:
        self.handlers[event] = handler

    def send(self, method: str):
        self.calls.append(method)
        if method == self.fail_method:
            raise RuntimeError("private vendor failure https://secret.test/?token=private")
        if method == "ServiceWorker.enable":
            if self.registrations is not None:
                self.handlers["ServiceWorker.workerRegistrationUpdated"](self.registrations)
            if self.versions is not None:
                self.handlers["ServiceWorker.workerVersionUpdated"](self.versions)
            for item in self.errors:
                self.handlers["ServiceWorker.workerErrorReported"](item)
            return {}
        if method == "ServiceWorker.disable":
            return {}
        if method == "Target.getTargets":
            return self.target_response
        raise AssertionError(f"unexpected CDP method {method}")

    def detach(self) -> None:
        self.detached = True


class _Page:
    def __init__(self) -> None:
        self.waits: list[int] = []

    def wait_for_timeout(self, milliseconds: int) -> None:
        self.waits.append(milliseconds)


class _Context:
    def __init__(self, pages: list[_Page], page_session: _Session) -> None:
        self.pages = pages
        self.page_session = page_session
        self.session_pages: list[_Page] = []

    def new_cdp_session(self, page: _Page) -> _Session:
        self.session_pages.append(page)
        return self.page_session


class _Browser:
    def __init__(self, browser_session: _Session, contexts: list[_Context]) -> None:
        self.browser_session = browser_session
        self.contexts = contexts
        self.closed = False
        self.close_reason = None

    def new_browser_cdp_session(self) -> _Session:
        return self.browser_session

    def close(self, *, reason: str | None = None) -> None:
        self.closed = True
        self.close_reason = reason


class _Chromium:
    def __init__(self, browser: _Browser) -> None:
        self.browser = browser
        self.calls: list[tuple[str, float, bool, bool]] = []

    def connect_over_cdp(
        self,
        endpoint: str,
        *,
        timeout: float,
        is_local: bool,
        no_defaults: bool,
    ) -> _Browser:
        self.calls.append((endpoint, timeout, is_local, no_defaults))
        return self.browser


class _PlaywrightContextManager:
    def __init__(self, chromium: _Chromium) -> None:
        self.playwright = SimpleNamespace(chromium=chromium)

    def __enter__(self):
        return self.playwright

    def __exit__(self, exc_type, exc, traceback) -> None:
        return None


def _fake_sync_api(
    browser_session: _Session,
    page_session: _Session | None = None,
    *,
    page_count: int = 1,
):
    contexts: list[_Context] = []
    pages: list[_Page] = []
    if page_count:
        if page_session is None:
            page_session = _Session()
        pages = [_Page() for _ in range(page_count)]
        contexts = [_Context(pages, page_session)]
    browser = _Browser(browser_session, contexts)
    chromium = _Chromium(browser)
    api = SimpleNamespace(sync_playwright=lambda: _PlaywrightContextManager(chromium))
    return api, chromium, browser, pages, page_session


def _sessions(*, stopped: bool = False) -> tuple[_Session, _Session]:
    target_id = None if stopped else "worker-1"
    worker_targets = []
    if target_id is not None:
        worker_targets.append(
            {
                "targetId": target_id,
                "type": "service_worker",
                "title": "private title",
                "url": "chrome-extension://abcdef/background.js?token=private#fragment",
                "attached": False,
            }
        )
    worker_targets.append(
        {
            "targetId": "page-1",
            "type": "page",
            "title": "private page title",
            "url": "https://example.test/path?token=private#fragment",
            "attached": False,
        }
    )
    version = {
        "versionId": "version-private",
        "registrationId": "registration-private",
        "scriptURL": "chrome-extension://abcdef/background.js?token=private",
        "runningStatus": "stopped" if stopped else "running",
        "status": "activated",
        "controlledClients": [] if stopped else ["client-private"],
    }
    if target_id is not None:
        version["targetId"] = target_id

    browser_session = _Session(target_response={"targetInfos": worker_targets})
    page_session = _Session(
        registrations={
            "registrations": [
                {
                    "registrationId": "registration-private",
                    "scopeURL": "chrome-extension://abcdef/?token=private",
                    "isDeleted": False,
                }
            ]
        },
        versions={"versions": [version]},
        errors=(
            {
                "errorMessage": {
                    "errorMessage": "private worker error text",
                    "registrationId": "registration-private",
                    "versionId": "version-private",
                    "sourceURL": "chrome-extension://abcdef/background.js?private",
                    "lineNumber": 1,
                    "columnNumber": 2,
                }
            },
        ),
    )
    return browser_session, page_session


def test_run_worker_diagnostics_is_read_only_sanitized_and_cleans_up(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    browser_session, page_session = _sessions()
    api, chromium, browser, pages, _ = _fake_sync_api(browser_session, page_session)
    monkeypatch.setattr(playwright_workers, "_load_playwright_sync_api", lambda: api)

    result = playwright_workers._run_worker_diagnostics("http://127.0.0.1:9222", 4.0)

    assert chromium.calls == [("http://127.0.0.1:9222", 3000.0, True, True)]
    assert browser_session.calls == ["Target.getTargets"]
    assert page_session.calls == ["ServiceWorker.enable", "ServiceWorker.disable"]
    assert pages[0].waits == [300]
    assert browser_session.detached is True
    assert page_session.detached is True
    assert browser.closed is True
    assert browser.close_reason == "host-ops read-only worker diagnostics complete"
    assert result.worker_targets[0].url == "chrome-extension:"
    assert result.registrations[0].scope_url == "chrome-extension:"
    assert result.versions[0].script_url == "chrome-extension:"
    assert result.versions[0].running_status == "running"
    assert result.versions[0].lifecycle_status == "activated"
    assert result.versions[0].controlled_client_count == 1
    assert result.versions[0].target_attached is False
    assert result.error_count == 1

    encoded = json.dumps(result.as_dict())
    for private_value in (
        "abcdef",
        "token",
        "private worker error text",
        "registration-private",
        "version-private",
        "client-private",
        "private page title",
    ):
        assert private_value not in encoded


def test_stopped_service_worker_is_visible_without_live_target(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    browser_session, page_session = _sessions(stopped=True)
    api, _chromium, _browser, _pages, _ = _fake_sync_api(browser_session, page_session)
    monkeypatch.setattr(playwright_workers, "_load_playwright_sync_api", lambda: api)

    result = playwright_workers._run_worker_diagnostics("http://127.0.0.1:9222", 4.0)

    assert result.worker_targets == ()
    assert len(result.versions) == 1
    assert result.versions[0].target_id is None
    assert result.versions[0].target_attached is None
    assert result.versions[0].running_status == "stopped"
    assert result.versions[0].scope_url == "chrome-extension:"


def test_malformed_service_worker_event_fails_closed_and_cleans_up(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    browser_session = _Session(target_response={"targetInfos": []})
    page_session = _Session(registrations={"registrations": [{"registrationId": "x"}]})
    api, _chromium, browser, _pages, _ = _fake_sync_api(browser_session, page_session)
    monkeypatch.setattr(playwright_workers, "_load_playwright_sync_api", lambda: api)

    with pytest.raises(BrowserWorkerDiagnosticsError, match="registration deleted state"):
        playwright_workers._run_worker_diagnostics("http://127.0.0.1:9222", 4.0)

    assert page_session.calls == ["ServiceWorker.enable", "ServiceWorker.disable"]
    assert page_session.detached is True
    assert browser_session.detached is True
    assert browser.closed is True


def test_target_query_failure_still_disables_detaches_and_disconnects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    browser_session = _Session(target_response={}, fail_method="Target.getTargets")
    page_session = _Session()
    api, _chromium, browser, _pages, _ = _fake_sync_api(browser_session, page_session)
    monkeypatch.setattr(playwright_workers, "_load_playwright_sync_api", lambda: api)

    with pytest.raises(RuntimeError, match="private vendor failure"):
        playwright_workers._run_worker_diagnostics("http://127.0.0.1:9222", 4.0)

    assert page_session.calls == ["ServiceWorker.enable", "ServiceWorker.disable"]
    assert page_session.detached is True
    assert browser_session.detached is True
    assert browser.closed is True


def test_no_pages_still_returns_worker_target_inventory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    browser_session = _Session(
        target_response={
            "targetInfos": [
                {
                    "targetId": "worker-1",
                    "type": "service_worker",
                    "url": "chrome-extension://abcdef/background.js",
                    "attached": False,
                }
            ]
        }
    )
    api, _chromium, browser, pages, _ = _fake_sync_api(browser_session, page_count=0)
    monkeypatch.setattr(playwright_workers, "_load_playwright_sync_api", lambda: api)

    result = playwright_workers._run_worker_diagnostics("http://127.0.0.1:9222", 4.0)

    assert pages == []
    assert len(result.worker_targets) == 1
    assert result.registrations == ()
    assert result.versions == ()
    assert browser_session.calls == ["Target.getTargets"]
    assert browser_session.detached is True
    assert browser.closed is True


def test_service_worker_enable_failure_fails_closed_and_detaches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    browser_session = _Session(target_response={"targetInfos": []})
    page_session = _Session(fail_method="ServiceWorker.enable")
    api, _chromium, browser, _pages, _ = _fake_sync_api(browser_session, page_session)
    monkeypatch.setattr(playwright_workers, "_load_playwright_sync_api", lambda: api)

    with pytest.raises(BrowserWorkerDiagnosticsError, match="unavailable for an attached page"):
        playwright_workers._run_worker_diagnostics("http://127.0.0.1:9222", 4.0)

    assert page_session.calls == ["ServiceWorker.enable"]
    assert page_session.detached is True
    assert browser_session.calls == []
    assert browser_session.detached is True
    assert browser.closed is True


def test_page_inventory_bound_fails_closed_before_page_sessions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    browser_session = _Session(target_response={"targetInfos": []})
    page_session = _Session()
    api, _chromium, browser, _pages, _ = _fake_sync_api(
        browser_session,
        page_session,
        page_count=129,
    )
    monkeypatch.setattr(playwright_workers, "_load_playwright_sync_api", lambda: api)

    with pytest.raises(BrowserWorkerDiagnosticsError, match="page inventory exceeded"):
        playwright_workers._run_worker_diagnostics("http://127.0.0.1:9222", 4.0)

    assert page_session.calls == []
    assert browser_session.detached is True
    assert browser.closed is True


def test_main_requires_endpoint_and_hides_unexpected_exception(
    monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    monkeypatch.delenv(playwright_workers._ATTACH_ENDPOINT_ENV, raising=False)
    assert playwright_workers.main(["--timeout", "3"]) == 2
    assert "endpoint is missing" in json.loads(capsys.readouterr().out)["error"]

    monkeypatch.setenv(playwright_workers._ATTACH_ENDPOINT_ENV, "http://127.0.0.1:9222")
    monkeypatch.setattr(
        playwright_workers,
        "_run_worker_diagnostics",
        lambda *_args: (_ for _ in ()).throw(RuntimeError("private details")),
    )
    assert playwright_workers.main(["--timeout", "3"]) == 1
    output = json.loads(capsys.readouterr().out)
    assert output == {"error": "browser worker diagnostics failed"}
    assert "private details" not in json.dumps(output)


def test_protocol_parsers_reject_invalid_shapes() -> None:
    with pytest.raises(BrowserWorkerDiagnosticsError, match="invalid target data"):
        playwright_workers._worker_targets_from_response({"targetInfos": {}})
    with pytest.raises(BrowserWorkerDiagnosticsError, match="registration event"):
        playwright_workers._merge_registrations({}, {})
    with pytest.raises(BrowserWorkerDiagnosticsError, match="version event"):
        playwright_workers._merge_versions({}, {})
