from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from local_agent.host_ops.capabilities.local.browser import (
    ManagedBrowserError,
    ManagedBrowserProbe,
    playwright_probe,
)


class _ConsoleError:
    type = "error"


class _Page:
    def __init__(
        self,
        final_url: str = "https://user:password@example.test:8443/final?x=1#f",
    ) -> None:
        self.url = final_url
        self.handlers = {}
        self.goto_calls = []

    def on(self, name, handler) -> None:
        self.handlers[name] = handler

    def goto(self, url: str, *, wait_until: str, timeout: float):
        self.goto_calls.append((url, wait_until, timeout))
        self.handlers["console"](_ConsoleError())
        self.handlers["pageerror"](RuntimeError("page failed"))
        self.handlers["requestfailed"](object())
        return SimpleNamespace(status=201)

    def title(self) -> str:
        return "Probe title"


class _Context:
    def __init__(self, page: _Page) -> None:
        self.page = page
        self.default_timeout = None
        self.navigation_timeout = None
        self.closed = False

    def set_default_timeout(self, timeout: float) -> None:
        self.default_timeout = timeout

    def set_default_navigation_timeout(self, timeout: float) -> None:
        self.navigation_timeout = timeout

    def new_page(self) -> _Page:
        return self.page

    def close(self) -> None:
        self.closed = True


class _Browser:
    def __init__(self, context: _Context) -> None:
        self.context = context
        self.closed = False

    def new_context(self, *, accept_downloads: bool) -> _Context:
        assert accept_downloads is False
        return self.context

    def close(self) -> None:
        self.closed = True


class _BrowserType:
    def __init__(self, browser: _Browser) -> None:
        self.browser = browser
        self.launch_calls = []

    def launch(self, *, headless: bool, timeout: float) -> _Browser:
        self.launch_calls.append((headless, timeout))
        return self.browser


class _PlaywrightContextManager:
    def __init__(self, browser_type: _BrowserType) -> None:
        self.playwright = SimpleNamespace(
            chromium=browser_type,
            firefox=browser_type,
            webkit=browser_type,
        )

    def __enter__(self):
        return self.playwright

    def __exit__(self, exc_type, exc, traceback) -> None:
        return None


def _fake_sync_api(page: _Page):
    context = _Context(page)
    browser = _Browser(context)
    browser_type = _BrowserType(browser)
    api = SimpleNamespace(sync_playwright=lambda: _PlaywrightContextManager(browser_type))
    return api, browser_type, browser, context


def _evidence() -> ManagedBrowserProbe:
    return ManagedBrowserProbe(
        engine="chromium",
        requested_url="https://example.test/start",
        final_url="https://example.test/final",
        title="Probe title",
        status_code=200,
        console_error_count=0,
        page_error_count=0,
        request_failure_count=0,
    )


def test_run_probe_uses_disposable_context_and_returns_sanitized_counts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    page = _Page()
    api, browser_type, browser, context = _fake_sync_api(page)
    monkeypatch.setattr(playwright_probe, "_load_playwright_sync_api", lambda: api)

    result = playwright_probe._run_probe(
        "https://example.test/start?token=secret#fragment",
        "chromium",
        4.0,
    )

    assert result == ManagedBrowserProbe(
        engine="chromium",
        requested_url="https://example.test/start",
        final_url="https://example.test:8443/final",
        title="Probe title",
        status_code=201,
        console_error_count=1,
        page_error_count=1,
        request_failure_count=1,
    )
    assert browser_type.launch_calls == [(True, 3000.0)]
    assert context.default_timeout == 3000.0
    assert context.navigation_timeout == 3000.0
    assert page.goto_calls == [
        ("https://example.test/start?token=secret#fragment", "domcontentloaded", 3000.0)
    ]
    assert context.closed is True
    assert browser.closed is True


def test_run_probe_fails_closed_on_non_http_final_url_and_still_closes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    page = _Page("file:///tmp/private-result")
    api, _browser_type, browser, context = _fake_sync_api(page)
    monkeypatch.setattr(playwright_probe, "_load_playwright_sync_api", lambda: api)

    with pytest.raises(ManagedBrowserError, match="non-HTTP"):
        playwright_probe._run_probe("https://example.test/start", "chromium", 4.0)

    assert context.closed is True
    assert browser.closed is True


def test_error_message_maps_missing_browser_binary_without_url_leak() -> None:
    message = playwright_probe._error_message(
        RuntimeError(
            "Executable doesn't exist while opening https://example.test/path?token=secret#fragment"
        ),
        engine="chromium",
        url="https://example.test/path?token=secret#fragment",
    )

    assert "python -m playwright install chromium" in message
    assert "secret" not in message


def test_main_emits_success_json(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    monkeypatch.setenv(playwright_probe._PROBE_URL_ENV, "https://example.test/start?token=secret")
    monkeypatch.setattr(playwright_probe, "_run_probe", lambda *_args: _evidence())

    assert playwright_probe.main(["--engine", "chromium", "--timeout", "3"]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload == _evidence().as_dict()


def test_main_missing_url_fails_with_structured_json(
    monkeypatch: pytest.MonkeyPatch,
    capsys,
) -> None:
    monkeypatch.delenv(playwright_probe._PROBE_URL_ENV, raising=False)

    assert playwright_probe.main(["--engine", "chromium", "--timeout", "3"]) == 2

    payload = json.loads(capsys.readouterr().out)
    assert payload == {"error": "managed browser probe URL is missing"}


def test_main_sanitizes_runtime_failure(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    target = "https://example.test/start?token=secret#fragment"
    monkeypatch.setenv(playwright_probe._PROBE_URL_ENV, target)

    def fail(*_args):
        raise RuntimeError(
            "failed after https://user:password@redirect.test/final?session=hidden#state"
        )

    monkeypatch.setattr(playwright_probe, "_run_probe", fail)

    assert playwright_probe.main(["--engine", "chromium", "--timeout", "3"]) == 1

    error = json.loads(capsys.readouterr().out)["error"]
    assert "password" not in error
    assert "hidden" not in error
    assert "https://redirect.test/final" in error
