from __future__ import annotations

import importlib
import json
from types import SimpleNamespace

import pytest

from local_agent.host_ops.capabilities.local.browser import BrowserAttachInspection, BrowserAttachTarget

playwright_attach = importlib.import_module("local_agent.host_ops.capabilities.local.browser.playwright_attach")


class _Session:
    def __init__(self, response: object, *, error: Exception | None = None) -> None:
        self.response = response
        self.error = error
        self.calls: list[str] = []
        self.detached = False

    def send(self, method: str):
        self.calls.append(method)
        if self.error is not None:
            raise self.error
        return self.response

    def detach(self) -> None:
        self.detached = True


class _Browser:
    version = "153.0.8010.12"

    def __init__(self, session: _Session) -> None:
        self.contexts = [object()]
        self.session = session
        self.closed = False
        self.close_reason = None

    def new_browser_cdp_session(self) -> _Session:
        return self.session

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


def _fake_sync_api(session: _Session):
    browser = _Browser(session)
    chromium = _Chromium(browser)
    api = SimpleNamespace(sync_playwright=lambda: _PlaywrightContextManager(chromium))
    return api, chromium, browser


def _evidence() -> BrowserAttachInspection:
    return BrowserAttachInspection(
        endpoint="http://127.0.0.1:9222",
        browser_version="153.0.8010.12",
        context_count=1,
        targets=(
            BrowserAttachTarget(
                target_id="page-1",
                target_type="page",
                title="Example",
                url="https://example.test/path",
                attached=False,
            ),
        ),
    )


def test_run_attach_uses_no_defaults_and_detaches_cleanly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _Session(
        {
            "targetInfos": [
                {
                    "targetId": "page-1",
                    "type": "page",
                    "title": "Example",
                    "url": "https://user:password@example.test/path?token=secret#fragment",
                    "attached": False,
                },
                {
                    "targetId": "worker-1",
                    "type": "service_worker",
                    "title": "Extension worker",
                    "url": "chrome-extension://abcdef/background.js?token=secret",
                    "attached": True,
                },
            ]
        }
    )
    api, chromium, browser = _fake_sync_api(session)
    monkeypatch.setattr(playwright_attach, "_load_playwright_sync_api", lambda: api)

    result = playwright_attach._run_attach("http://127.0.0.1:9222", 4.0)

    assert chromium.calls == [("http://127.0.0.1:9222", 3000.0, True, True)]
    assert session.calls == ["Target.getTargets"]
    assert session.detached is True
    assert browser.closed is True
    assert browser.close_reason == "host-ops read-only CDP attach complete"
    assert result.context_count == 1
    assert result.targets[0].url == "https://example.test/path"
    assert result.targets[1].url == "chrome-extension:"
    assert "secret" not in json.dumps(result.as_dict())
    assert "password" not in json.dumps(result.as_dict())


def test_run_attach_detaches_and_disconnects_when_target_query_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _Session({}, error=RuntimeError("Target.getTargets failed"))
    api, _chromium, browser = _fake_sync_api(session)
    monkeypatch.setattr(playwright_attach, "_load_playwright_sync_api", lambda: api)

    with pytest.raises(RuntimeError, match=r"Target.getTargets failed"):
        playwright_attach._run_attach("http://127.0.0.1:9222", 4.0)

    assert session.detached is True
    assert browser.closed is True


def test_targets_are_sorted_and_fail_closed_on_invalid_protocol_shape() -> None:
    result = playwright_attach._targets_from_response(
        {
            "targetInfos": [
                {
                    "targetId": "z",
                    "type": "service_worker",
                    "title": "Worker",
                    "url": "https://worker.test/sw.js",
                    "attached": True,
                },
                {
                    "targetId": "a",
                    "type": "page",
                    "title": "Page",
                    "url": "https://page.test/",
                    "attached": False,
                },
            ]
        }
    )

    assert [(target.target_type, target.target_id) for target in result] == [
        ("page", "a"),
        ("service_worker", "z"),
    ]

    with pytest.raises(Exception, match="invalid target list"):
        playwright_attach._targets_from_response({"targetInfos": {}})


def test_main_emits_success_json(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    monkeypatch.setenv(
        playwright_attach._ATTACH_ENDPOINT_ENV,
        "http://127.0.0.1:9222",
    )
    monkeypatch.setattr(playwright_attach, "_run_attach", lambda *_args: _evidence())

    assert playwright_attach.main(["--timeout", "3"]) == 0

    assert json.loads(capsys.readouterr().out) == _evidence().as_dict()


def test_main_requires_endpoint(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    monkeypatch.delenv(playwright_attach._ATTACH_ENDPOINT_ENV, raising=False)

    assert playwright_attach.main(["--timeout", "3"]) == 2

    assert json.loads(capsys.readouterr().out) == {
        "error": "browser CDP attach endpoint is missing"
    }


def test_runtime_error_redacts_http_and_websocket_urls() -> None:
    error = playwright_attach._error_message(
        RuntimeError(
            "connect http://127.0.0.1:9222/json/version then "
            "ws://127.0.0.1:9222/devtools/browser/private-id"
        )
    )

    assert "private-id" not in error
    assert "/json/version" not in error
    assert "<redacted-url>" in error
    assert "<redacted-cdp-url>" in error


def test_optional_playwright_import_fails_with_actionable_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def missing(_name: str):
        raise ModuleNotFoundError("No module named 'playwright'", name="playwright")

    monkeypatch.setattr(playwright_attach.importlib, "import_module", missing)

    with pytest.raises(Exception, match=r"host-ops\[browser\]"):
        playwright_attach._load_playwright_sync_api()


def test_targets_reject_non_object_response() -> None:
    with pytest.raises(Exception, match="non-object response"):
        playwright_attach._targets_from_response([])


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        (None, "invalid target"),
        (
            {
                "targetId": "",
                "type": "page",
                "title": "x",
                "url": "https://example.test/",
                "attached": False,
            },
            "invalid id",
        ),
        (
            {
                "targetId": "x",
                "type": "",
                "title": "x",
                "url": "https://example.test/",
                "attached": False,
            },
            "invalid type",
        ),
        (
            {
                "targetId": "x",
                "type": "page",
                "title": "x",
                "url": "https://example.test/",
                "attached": "no",
            },
            "invalid attached state",
        ),
    ],
)
def test_target_info_validation_fails_closed(raw: object, message: str) -> None:
    with pytest.raises(Exception, match=message):
        playwright_attach._target_from_info(raw)


def test_main_rejects_invalid_timeout(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    monkeypatch.setenv(playwright_attach._ATTACH_ENDPOINT_ENV, "http://127.0.0.1:9222")

    assert playwright_attach.main(["--timeout", "0.5"]) == 2
    assert "timeout is invalid" in json.loads(capsys.readouterr().out)["error"]


def test_main_sanitizes_runtime_failure(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    monkeypatch.setenv(playwright_attach._ATTACH_ENDPOINT_ENV, "http://127.0.0.1:9222")

    def fail(*_args):
        raise RuntimeError(
            "connect http://127.0.0.1:9222/json/version via "
            "ws://127.0.0.1:9222/devtools/browser/private-id"
        )

    monkeypatch.setattr(playwright_attach, "_run_attach", fail)

    assert playwright_attach.main(["--timeout", "3"]) == 1
    error = json.loads(capsys.readouterr().out)["error"]
    assert "private-id" not in error
    assert "<redacted-cdp-url>" in error


def test_browser_attach_error_message_is_preserved() -> None:
    error = playwright_attach._error_message(
        playwright_attach.BrowserAttachError("bounded attach error")
    )
    assert error == "bounded attach error"
