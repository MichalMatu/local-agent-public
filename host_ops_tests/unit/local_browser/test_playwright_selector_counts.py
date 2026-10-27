from __future__ import annotations

import importlib
import json
from types import SimpleNamespace

import pytest

playwright_counts = importlib.import_module(
    "local_agent.host_ops.capabilities.local.browser.playwright_selector_counts"
)


class Session:
    def __init__(self, target_id: str) -> None:
        self.target_id = target_id
        self.detached = False
        self.calls = []

    def send(self, method: str, params=None):
        self.calls.append((method, params))
        if method == "Target.getTargetInfo":
            return {
                "targetInfo": {
                    "targetId": self.target_id,
                    "type": "page",
                    "title": "Example",
                    "url": "https://example.test/path?token=value#fragment",
                }
            }
        if method == "DOM.getDocument":
            assert params == {"depth": 0, "pierce": False}
            return {"root": {"nodeId": 7}}
        if method == "DOM.querySelectorAll":
            if params["selector"] == "#prompt-textarea":
                return {"nodeIds": [11]}
            return {"nodeIds": []}
        raise AssertionError(method)

    def detach(self):
        self.detached = True


class Context:
    def __init__(self, ids):
        self.pages = [object() for _ in ids]
        self.sessions = [Session(value) for value in ids]

    def new_cdp_session(self, page):
        return self.sessions[self.pages.index(page)]


class Browser:
    def __init__(self, ids):
        self.contexts = [Context(ids)]
        self.closed = False

    def close(self, *, reason=None):
        self.closed = True


class Chromium:
    def __init__(self, browser):
        self.browser = browser

    def connect_over_cdp(self, endpoint, *, timeout, is_local, no_defaults):
        assert endpoint == "http://127.0.0.1:9222"
        assert is_local is True and no_defaults is True
        return self.browser


class PlaywrightCM:
    def __init__(self, chromium):
        self.value = SimpleNamespace(chromium=chromium)

    def __enter__(self):
        return self.value

    def __exit__(self, *args):
        return None


def test_count_matches_exact_target_without_runtime_evaluate(monkeypatch) -> None:
    browser = Browser(["other", "wanted"])
    api = SimpleNamespace(sync_playwright=lambda: PlaywrightCM(Chromium(browser)))
    monkeypatch.setattr(playwright_counts, "_load_playwright_sync_api", lambda: api)

    result = playwright_counts._run_count(
        "http://127.0.0.1:9222",
        "wanted",
        ("#prompt-textarea", "#composer-submit-button"),
        4,
    )

    assert [item.match_count for item in result.selectors] == [1, 0]
    assert result.url == "https://example.test/path"
    assert all(session.detached for session in browser.contexts[0].sessions)
    assert browser.closed is True
    methods = [method for session in browser.contexts[0].sessions for method, _ in session.calls]
    assert "Runtime.evaluate" not in methods
    assert "DOM.querySelectorAll" in methods


def test_missing_target_disconnects(monkeypatch) -> None:
    browser = Browser(["other"])
    api = SimpleNamespace(sync_playwright=lambda: PlaywrightCM(Chromium(browser)))
    monkeypatch.setattr(playwright_counts, "_load_playwright_sync_api", lambda: api)
    with pytest.raises(Exception, match="not an attached page target"):
        playwright_counts._run_count("http://127.0.0.1:9222", "wanted", ("#prompt-textarea",), 4)
    assert browser.contexts[0].sessions[0].detached is True
    assert browser.closed is True


def test_protocol_validation_and_selector_errors(monkeypatch) -> None:
    with pytest.raises(Exception, match="invalid root data"):
        playwright_counts._document_root({})
    with pytest.raises(Exception, match="invalid node id"):
        playwright_counts._document_root({"root": {"nodeId": True}})

    class BadSession:
        def send(self, *_args, **_kwargs):
            return {"nodeIds": "bad"}

    with pytest.raises(Exception, match="invalid data"):
        playwright_counts._count_selector(BadSession(), 1, "#x", 0)

    class RaisingSession:
        def send(self, *_args, **_kwargs):
            raise RuntimeError("detail")

    with pytest.raises(Exception, match="index 2"):
        playwright_counts._count_selector(RaisingSession(), 1, "#x", 2)

    class ManySession:
        def send(self, *_args, **_kwargs):
            return {"nodeIds": [1, 2]}

    monkeypatch.setattr(playwright_counts, "_MAX_MATCHES_PER_SELECTOR", 1)
    with pytest.raises(Exception, match="exceeded its bound"):
        playwright_counts._count_selector(ManySession(), 1, "#x", 0)


def test_run_rejects_non_page_target_after_match(monkeypatch) -> None:
    browser = Browser(["wanted"])
    api = SimpleNamespace(sync_playwright=lambda: PlaywrightCM(Chromium(browser)))
    monkeypatch.setattr(playwright_counts, "_load_playwright_sync_api", lambda: api)
    calls = 0
    original = playwright_counts._target_info

    def target_info(response):
        nonlocal calls
        calls += 1
        info = original(response)
        if calls == 2:
            return {**info, "type": "worker"}
        return info

    monkeypatch.setattr(playwright_counts, "_target_info", target_info)
    with pytest.raises(Exception, match="not a page target"):
        playwright_counts._run_count("http://127.0.0.1:9222", "wanted", ("#x",), 4)
    assert browser.closed is True


def test_run_rejects_empty_target_url(monkeypatch) -> None:
    browser = Browser(["wanted"])
    browser.contexts[0].sessions[0].send = lambda method, params=None: (
        {
            "targetInfo": {
                "targetId": "wanted",
                "type": "page",
                "title": "Example",
                "url": "",
            }
        }
        if method == "Target.getTargetInfo"
        else {"root": {"nodeId": 7}}
        if method == "DOM.getDocument"
        else {"nodeIds": []}
    )
    api = SimpleNamespace(sync_playwright=lambda: PlaywrightCM(Chromium(browser)))
    monkeypatch.setattr(playwright_counts, "_load_playwright_sync_api", lambda: api)
    with pytest.raises(Exception, match="invalid URL"):
        playwright_counts._run_count("http://127.0.0.1:9222", "wanted", ("#x",), 4)


def test_load_selectors_and_main_input_validation(monkeypatch, capsys) -> None:
    assert playwright_counts._load_selectors('["#a", "#a", "#b"]') == ("#a", "#b")
    with pytest.raises(Exception, match="invalid JSON"):
        playwright_counts._load_selectors("not-json")
    with pytest.raises(Exception, match="JSON array"):
        playwright_counts._load_selectors("{}")

    monkeypatch.delenv("HOST_OPS_BROWSER_ATTACH_ENDPOINT", raising=False)
    monkeypatch.delenv("HOST_OPS_BROWSER_ATTACH_TARGET_ID", raising=False)
    monkeypatch.delenv(playwright_counts._SELECTORS_ENV, raising=False)
    assert playwright_counts.main(["--timeout", "3"]) == 2
    assert "missing" in json.loads(capsys.readouterr().out)["error"]


def test_main_handles_timeout_expected_unexpected_and_success(monkeypatch, capsys) -> None:
    monkeypatch.setenv("HOST_OPS_BROWSER_ATTACH_ENDPOINT", "http://127.0.0.1:9222")
    monkeypatch.setenv("HOST_OPS_BROWSER_ATTACH_TARGET_ID", "page-1")
    monkeypatch.setenv(playwright_counts._SELECTORS_ENV, '["#x"]')

    assert playwright_counts.main(["--timeout", "0"]) == 2
    assert "timeout" in json.loads(capsys.readouterr().out)["error"]

    def expected(*_args):
        raise playwright_counts.BrowserSelectorCountError("bounded failure")

    monkeypatch.setattr(playwright_counts, "_run_count", expected)
    assert playwright_counts.main(["--timeout", "3"]) == 1
    assert json.loads(capsys.readouterr().out)["error"] == "bounded failure"

    def unexpected(*_args):
        raise RuntimeError("private detail")

    monkeypatch.setattr(playwright_counts, "_run_count", unexpected)
    assert playwright_counts.main(["--timeout", "3"]) == 1
    assert json.loads(capsys.readouterr().out)["error"] == "browser selector count failed"

    evidence = SimpleNamespace(as_dict=lambda: {"target_id": "page-1"})
    monkeypatch.setattr(playwright_counts, "_run_count", lambda *_args: evidence)
    assert playwright_counts.main(["--timeout", "3"]) == 0
    assert json.loads(capsys.readouterr().out)["target_id"] == "page-1"
