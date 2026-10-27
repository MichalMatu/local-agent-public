from __future__ import annotations

import importlib
from types import SimpleNamespace

import pytest

from local_agent.host_ops.capabilities.local.browser import BrowserWorkerDiagnosticsError

playwright_workers = importlib.import_module(
    "local_agent.host_ops.capabilities.local.browser.playwright_worker_diagnostics"
)


def test_load_playwright_reports_missing_optional_dependency(monkeypatch) -> None:
    error = ModuleNotFoundError("No module named 'playwright'")
    error.name = "playwright"

    def missing(_name: str):
        raise error

    monkeypatch.setattr(playwright_workers.importlib, "import_module", missing)
    with pytest.raises(BrowserWorkerDiagnosticsError, match="not installed"):
        playwright_workers._load_playwright_sync_api()


def test_identifier_and_url_validation_fail_closed() -> None:
    with pytest.raises(BrowserWorkerDiagnosticsError, match="target id"):
        playwright_workers._bounded_id(4, "target id")
    with pytest.raises(BrowserWorkerDiagnosticsError, match="target id"):
        playwright_workers._bounded_id("bad id!", "target id")
    with pytest.raises(BrowserWorkerDiagnosticsError, match="URL"):
        playwright_workers._sanitized_url(None, "URL")


def test_worker_target_parser_rejects_invalid_entries(monkeypatch) -> None:
    with pytest.raises(BrowserWorkerDiagnosticsError, match="invalid target"):
        playwright_workers._worker_targets_from_response({"targetInfos": [None]})

    base = {
        "targetId": "worker-1",
        "type": "service_worker",
        "url": "chrome-extension://abcdef/background.js",
        "attached": False,
    }
    for key, value, message in (
        ("type", 4, "invalid type"),
        ("attached", "false", "attached state"),
        ("url", "", "target URL"),
    ):
        item = dict(base)
        item[key] = value
        with pytest.raises(BrowserWorkerDiagnosticsError, match=message):
            playwright_workers._worker_targets_from_response({"targetInfos": [item]})

    monkeypatch.setattr(playwright_workers, "_MAX_WORKER_TARGETS", 0)
    with pytest.raises(BrowserWorkerDiagnosticsError, match="inventory exceeded"):
        playwright_workers._worker_targets_from_response({"targetInfos": [base]})


def test_registration_parser_rejects_invalid_entries_and_bounds(monkeypatch) -> None:
    with pytest.raises(BrowserWorkerDiagnosticsError, match="registration is invalid"):
        playwright_workers._merge_registrations({"registrations": [None]}, {})

    with pytest.raises(BrowserWorkerDiagnosticsError, match="deleted state"):
        playwright_workers._merge_registrations(
            {
                "registrations": [
                    {
                        "registrationId": "reg-1",
                        "scopeURL": "https://example.test/",
                        "isDeleted": 0,
                    }
                ]
            },
            {},
        )

    monkeypatch.setattr(playwright_workers, "_MAX_REGISTRATIONS", 0)
    with pytest.raises(BrowserWorkerDiagnosticsError, match="registrations exceeded"):
        playwright_workers._merge_registrations(
            {
                "registrations": [
                    {
                        "registrationId": "reg-1",
                        "scopeURL": "https://example.test/",
                        "isDeleted": False,
                    }
                ]
            },
            {},
        )


def _version(**overrides):
    value = {
        "versionId": "version-1",
        "registrationId": "reg-1",
        "scriptURL": "https://example.test/sw.js",
        "runningStatus": "running",
        "status": "activated",
        "controlledClients": ["client-1"],
        "targetId": "worker-1",
    }
    value.update(overrides)
    return value


def test_version_parser_rejects_invalid_status_clients_and_bounds(monkeypatch) -> None:
    with pytest.raises(BrowserWorkerDiagnosticsError, match="version is invalid"):
        playwright_workers._merge_versions({"versions": [None]}, {})

    for overrides, message in (
        ({"runningStatus": "awake"}, "running status"),
        ({"status": "ready"}, "lifecycle status"),
        ({"controlledClients": {}}, "controlled clients"),
        ({"controlledClients": ["bad id!"]}, "controlled client id"),
        ({"targetId": "bad id!"}, "version target id"),
    ):
        with pytest.raises(BrowserWorkerDiagnosticsError, match=message):
            playwright_workers._merge_versions(
                {"versions": [_version(**overrides)]},
                {},
            )

    monkeypatch.setattr(playwright_workers, "_MAX_VERSIONS", 0)
    with pytest.raises(BrowserWorkerDiagnosticsError, match="versions exceeded"):
        playwright_workers._merge_versions({"versions": [_version()]}, {})


def test_error_event_validation_and_bound_are_fail_closed(monkeypatch) -> None:
    class BrowserSession:
        def __init__(self) -> None:
            self.detached = False

        def send(self, method):
            if method == "Target.getTargets":
                return {"targetInfos": []}
            raise AssertionError(method)

        def detach(self) -> None:
            self.detached = True

    class PageSession:
        def __init__(self) -> None:
            self.handlers = {}
            self.detached = False

        def on(self, event, handler) -> None:
            self.handlers[event] = handler

        def send(self, method):
            if method == "ServiceWorker.enable":
                self.handlers["ServiceWorker.workerErrorReported"]({})
                return {}
            if method == "ServiceWorker.disable":
                return {}
            raise AssertionError(method)

        def detach(self) -> None:
            self.detached = True

    class Page:
        def wait_for_timeout(self, _milliseconds: int) -> None:
            return None

    class BrowserContext:
        def __init__(self, page_session: PageSession) -> None:
            self.pages = [Page()]
            self.page_session = page_session

        def new_cdp_session(self, _page: Page):
            return self.page_session

    class Browser:
        def __init__(self) -> None:
            self.browser_session = BrowserSession()
            self.page_session = PageSession()
            self.contexts = [BrowserContext(self.page_session)]
            self.closed = False

        def new_browser_cdp_session(self):
            return self.browser_session

        def close(self, *, reason=None) -> None:
            self.closed = True

    browser = Browser()
    chromium = SimpleNamespace(
        connect_over_cdp=lambda *args, **kwargs: browser,
    )

    class Context:
        def __enter__(self):
            return SimpleNamespace(chromium=chromium)

        def __exit__(self, *args):
            return None

    monkeypatch.setattr(
        playwright_workers,
        "_load_playwright_sync_api",
        lambda: SimpleNamespace(sync_playwright=lambda: Context()),
    )
    with pytest.raises(BrowserWorkerDiagnosticsError, match="error event is invalid"):
        playwright_workers._run_worker_diagnostics("http://127.0.0.1:9222", 3.0)
    assert browser.page_session.detached is True
    assert browser.browser_session.detached is True
    assert browser.closed is True


def test_main_rejects_invalid_timeout_and_reports_typed_error(monkeypatch, capsys) -> None:
    monkeypatch.setenv(
        playwright_workers._ATTACH_ENDPOINT_ENV,
        "http://127.0.0.1:9222",
    )
    assert playwright_workers.main(["--timeout", "0.5"]) == 2
    assert "timeout is invalid" in capsys.readouterr().out

    def fail(*_args):
        raise BrowserWorkerDiagnosticsError("bounded failure")

    monkeypatch.setattr(playwright_workers, "_run_worker_diagnostics", fail)
    assert playwright_workers.main(["--timeout", "3"]) == 1
    assert "bounded failure" in capsys.readouterr().out
