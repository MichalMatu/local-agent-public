from __future__ import annotations

import importlib
import json
from types import SimpleNamespace

import pytest

playwright_reload = importlib.import_module("local_agent.host_ops.capabilities.local.browser.playwright_reload")


class ExpectedEvent:
    def __init__(self, page: Page, event: str, timeout: float) -> None:
        self.page = page
        self.event = event
        self.timeout = timeout

    def __enter__(self):
        self.page.expect_calls.append((self.event, self.timeout))
        return self

    def __exit__(self, *args):
        return None


class Page:
    def __init__(self) -> None:
        self.expect_calls = []

    def expect_event(self, event: str, *, timeout: float) -> ExpectedEvent:
        return ExpectedEvent(self, event, timeout)


class Session:
    def __init__(self, target_id: str, *, target_type: str = "page") -> None:
        self.target_id = target_id
        self.target_type = target_type
        self.detached = False
        self.reloaded = False
        self.calls = []

    def send(self, method: str, params=None):
        self.calls.append((method, params))
        if method == "Target.getTargetInfo":
            return {
                "targetInfo": {
                    "targetId": self.target_id,
                    "type": self.target_type,
                    "title": "After" if self.reloaded else "Before",
                    "url": "https://user:pass@example.test/path?token=secret#fragment",
                }
            }
        if method == "Page.reload":
            self.reloaded = True
            return {}
        raise AssertionError(method)

    def detach(self) -> None:
        self.detached = True


class Context:
    def __init__(self, ids, *, wanted_type: str = "page") -> None:
        self.pages = [Page() for _ in ids]
        self.sessions = [
            Session(target_id, target_type=wanted_type if target_id == "wanted" else "page")
            for target_id in ids
        ]

    def new_cdp_session(self, page):
        return self.sessions[self.pages.index(page)]


class Browser:
    def __init__(self, ids, *, wanted_type: str = "page") -> None:
        self.contexts = [Context(ids, wanted_type=wanted_type)]
        self.closed = False

    def close(self, *, reason=None) -> None:
        self.closed = True


class Chromium:
    def __init__(self, browser: Browser) -> None:
        self.browser = browser
        self.calls = []

    def connect_over_cdp(self, endpoint, *, timeout, is_local, no_defaults):
        self.calls.append((endpoint, timeout, is_local, no_defaults))
        return self.browser


class PlaywrightCM:
    def __init__(self, chromium: Chromium) -> None:
        self.value = SimpleNamespace(chromium=chromium)

    def __enter__(self):
        return self.value

    def __exit__(self, *args):
        return None


def _install_fake(monkeypatch, browser: Browser) -> Chromium:
    chromium = Chromium(browser)
    monkeypatch.setattr(
        playwright_reload,
        "_load_playwright_sync_api",
        lambda: SimpleNamespace(sync_playwright=lambda: PlaywrightCM(chromium)),
    )
    return chromium


def test_reload_matches_exact_target_guards_url_and_reloads_once(monkeypatch) -> None:
    browser = Browser(["other", "wanted"])
    chromium = _install_fake(monkeypatch, browser)

    result = playwright_reload._run_reload(
        "http://127.0.0.1:9222",
        "wanted",
        "https://example.test/path",
        4,
    )

    context = browser.contexts[0]
    wanted = context.sessions[1]
    assert chromium.calls == [("http://127.0.0.1:9222", 3000.0, True, True)]
    assert sum(session.calls.count(("Page.reload", None)) for session in context.sessions) == 1
    assert wanted.calls == [
        ("Target.getTargetInfo", None),
        ("Target.getTargetInfo", None),
        ("Page.reload", None),
        ("Target.getTargetInfo", None),
    ]
    assert context.pages[1].expect_calls == [("load", 3000.0)]
    assert all(session.detached for session in context.sessions)
    assert browser.closed is True
    assert result.before_url == "https://example.test/path"
    assert result.after_url == "https://example.test/path"
    assert result.before_title == "Before"
    assert result.after_title == "After"
    serialized = json.dumps(result.as_dict())
    assert "secret" not in serialized and "pass" not in serialized


def test_reload_url_mismatch_fails_before_page_reload(monkeypatch) -> None:
    browser = Browser(["wanted"])
    _install_fake(monkeypatch, browser)

    with pytest.raises(Exception, match="does not match expected URL"):
        playwright_reload._run_reload(
            "http://127.0.0.1:9222",
            "wanted",
            "https://other.test/path",
            4,
        )

    session = browser.contexts[0].sessions[0]
    assert ("Page.reload", None) not in session.calls
    assert browser.contexts[0].pages[0].expect_calls == []
    assert session.detached is True
    assert browser.closed is True


def test_reload_rejects_missing_and_non_page_targets_without_reload(monkeypatch) -> None:
    missing = Browser(["other"])
    _install_fake(monkeypatch, missing)
    with pytest.raises(Exception, match="not an attached page target"):
        playwright_reload._run_reload(
            "http://127.0.0.1:9222", "wanted", "https://example.test/path", 4
        )
    assert all(
        ("Page.reload", None) not in session.calls for session in missing.contexts[0].sessions
    )

    non_page = Browser(["wanted"], wanted_type="worker")
    _install_fake(monkeypatch, non_page)
    with pytest.raises(Exception, match="not a page target"):
        playwright_reload._run_reload(
            "http://127.0.0.1:9222", "wanted", "https://example.test/path", 4
        )
    assert ("Page.reload", None) not in non_page.contexts[0].sessions[0].calls


def test_http_target_url_rejects_non_http_target() -> None:
    with pytest.raises(Exception, match=r"valid HTTP\(S\) URL"):
        playwright_reload._http_target_url({"url": "chrome-extension://abc/page.html"})


def test_main_validates_inputs_and_reports_bounded_errors(monkeypatch, capsys) -> None:
    for key in (
        "HOST_OPS_BROWSER_ATTACH_ENDPOINT",
        "HOST_OPS_BROWSER_ATTACH_TARGET_ID",
        playwright_reload._RELOAD_EXPECTED_URL_ENV,
    ):
        monkeypatch.delenv(key, raising=False)
    assert playwright_reload.main(["--timeout", "3"]) == 2
    assert "missing" in json.loads(capsys.readouterr().out)["error"]

    monkeypatch.setenv("HOST_OPS_BROWSER_ATTACH_ENDPOINT", "http://127.0.0.1:9222")
    monkeypatch.setenv("HOST_OPS_BROWSER_ATTACH_TARGET_ID", "page-1")
    monkeypatch.setenv(playwright_reload._RELOAD_EXPECTED_URL_ENV, "https://example.test/path")
    assert playwright_reload.main(["--timeout", "0.5"]) == 2
    assert "timeout" in json.loads(capsys.readouterr().out)["error"]

    def expected_error(*_args):
        raise playwright_reload.BrowserReloadError("guard failed")

    monkeypatch.setattr(playwright_reload, "_run_reload", expected_error)
    assert playwright_reload.main(["--timeout", "3"]) == 1
    assert json.loads(capsys.readouterr().out)["error"] == "guard failed"

    def unexpected_error(*_args):
        raise RuntimeError("private detail")

    monkeypatch.setattr(playwright_reload, "_run_reload", unexpected_error)
    assert playwright_reload.main(["--timeout", "3"]) == 1
    assert json.loads(capsys.readouterr().out)["error"] == "browser reload failed"
