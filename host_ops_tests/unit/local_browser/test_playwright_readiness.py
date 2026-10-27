from __future__ import annotations

import importlib
import json
from types import SimpleNamespace

import pytest

playwright_readiness = importlib.import_module(
    "local_agent.host_ops.capabilities.local.browser.playwright_readiness"
)

DIGEST_A = "a" * 64
DIGEST_B = "b" * 64
EXTENSION_ID = "abcdefghijklmnopabcdefghijklmnop"


class Page:
    def __init__(self) -> None:
        self.waits = []

    def wait_for_timeout(self, value):
        self.waits.append(value)


class Session:
    def __init__(
        self, target_id: str, *, url: str = "https://example.test/path?token=private"
    ) -> None:
        self.target_id = target_id
        self.url = url
        self.detached = False
        self.calls = []
        self.handlers = {}
        self.target_info_calls = 0
        self.script_events = []
        self.worker_targets = []

    def on(self, event, handler) -> None:
        self.handlers[event] = handler

    def send(self, method: str, params=None):
        self.calls.append((method, params))
        if method == "Target.getTargetInfo":
            self.target_info_calls += 1
            return {
                "targetInfo": {
                    "targetId": self.target_id,
                    "type": "page",
                    "title": "Example",
                    "url": self.url,
                }
            }
        if method == "DOM.getDocument":
            return {"root": {"nodeId": 7}}
        if method == "DOM.querySelectorAll":
            return {"nodeIds": [11]} if params["selector"] == "#ready" else {"nodeIds": []}
        if method == "Debugger.enable":
            for event in self.script_events:
                self.handlers["Debugger.scriptParsed"](event)
            return {}
        if method == "Debugger.disable":
            return {}
        if method == "Target.getTargets":
            return {"targetInfos": list(self.worker_targets)}
        raise AssertionError(method)

    def detach(self):
        self.detached = True


class Context:
    def __init__(self, ids):
        self.pages = [Page() for _ in ids]
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


def _install_fake(monkeypatch, browser) -> None:
    api = SimpleNamespace(sync_playwright=lambda: PlaywrightCM(Chromium(browser)))
    monkeypatch.setattr(playwright_readiness, "_load_playwright_sync_api", lambda: api)


def _script(name: str, digest: str = DIGEST_A, extension_id: str = EXTENSION_ID):
    return {
        "url": f"chrome-extension://{extension_id}/{name}",
        "hash": digest,
        "executionContextId": 5,
    }


def _worker(extension_id: str = EXTENSION_ID):
    return {
        "targetId": "worker-1",
        "type": "service_worker",
        "url": f"chrome-extension://{extension_id}/service_worker.js",
        "attached": True,
    }


def test_extension_identity_accepts_only_bounded_extension_script_urls() -> None:
    assert playwright_readiness._extension_identity(
        f"chrome-extension://{EXTENSION_ID}/dir/content.js"
    ) == (EXTENSION_ID, "content.js")
    for value in (
        "https://example.test/content.js",
        f"chrome-extension://{EXTENSION_ID}/",
        f"chrome-extension://{EXTENSION_ID}/content.js?x=1",
        f"chrome-extension://{EXTENSION_ID}/content.js#x",
        None,
    ):
        assert playwright_readiness._extension_identity(value) is None


def test_script_inventory_groups_only_requested_basenames() -> None:
    inventory, invalid = playwright_readiness._script_inventory(
        [
            _script("content.js"),
            _script("other.js", DIGEST_B),
            {"url": "https://example.test/app.js", "hash": DIGEST_A},
        ],
        (("content.js", DIGEST_A),),
    )
    assert invalid is False
    assert inventory == {EXTENSION_ID: {"content.js": {DIGEST_A}}}

    inventory, invalid = playwright_readiness._script_inventory(
        [_script("content.js", "short")], (("content.js", DIGEST_A),)
    )
    assert inventory == {}
    assert invalid is True


@pytest.mark.parametrize(
    ("inventory", "fingerprints", "state", "statuses", "origin"),
    [
        ({}, (("content.js", DIGEST_A),), "missing", ["missing"], None),
        (
            {EXTENSION_ID: {"content.js": {DIGEST_A}}},
            (("content.js", DIGEST_A),),
            "ready",
            ["matched"],
            EXTENSION_ID,
        ),
        (
            {EXTENSION_ID: {"content.js": {DIGEST_B}}},
            (("content.js", DIGEST_A),),
            "stale",
            ["stale"],
            EXTENSION_ID,
        ),
        (
            {EXTENSION_ID: {"content.js": {DIGEST_A}}},
            (("content.js", DIGEST_A), ("guard.js", DIGEST_B)),
            "missing",
            ["matched", "missing"],
            EXTENSION_ID,
        ),
        (
            {
                "a" * 32: {"content.js": {DIGEST_A}},
                "b" * 32: {"content.js": {DIGEST_A}},
            },
            (("content.js", DIGEST_A),),
            "ambiguous",
            ["matched"],
            None,
        ),
    ],
)
def test_content_script_classification(inventory, fingerprints, state, statuses, origin) -> None:
    evidence, actual_state, actual_origin = playwright_readiness._classify_content_scripts(
        inventory, fingerprints
    )
    assert actual_state == state
    assert actual_origin == origin
    assert [item.status for item in evidence] == statuses


def test_worker_state_correlates_hidden_extension_origin() -> None:
    assert (
        playwright_readiness._worker_state({"targetInfos": [_worker()]}, EXTENSION_ID) == "running"
    )
    assert playwright_readiness._worker_state({"targetInfos": []}, EXTENSION_ID) == "inactive"
    assert playwright_readiness._worker_state({"targetInfos": [_worker()]}, None) == "unknown"
    with pytest.raises(Exception, match="invalid data"):
        playwright_readiness._worker_state({}, EXTENSION_ID)


@pytest.mark.parametrize(
    ("dom_ready", "content", "worker", "diagnosis"),
    [
        (False, "ready", "running", "dom_not_ready"),
        (True, "missing", "unknown", "content_script_missing"),
        (True, "stale", "inactive", "content_script_stale"),
        (True, "ambiguous", "unknown", "extension_ambiguous"),
        (True, "ready", "inactive", "worker_inactive"),
        (True, "ready", "running", "ready"),
    ],
)
def test_diagnosis_priority(dom_ready, content, worker, diagnosis) -> None:
    assert playwright_readiness._diagnosis(dom_ready, content, worker) == diagnosis


def test_run_readiness_correlates_exact_target_without_runtime_evaluate(monkeypatch) -> None:
    browser = Browser(["other", "wanted"])
    wanted = browser.contexts[0].sessions[1]
    wanted.script_events = [_script("content.js")]
    wanted.worker_targets = [_worker()]
    _install_fake(monkeypatch, browser)

    result = playwright_readiness._run_readiness(
        "http://127.0.0.1:9222",
        "wanted",
        ("#ready",),
        (("content.js", DIGEST_A),),
        4,
    )

    assert result.dom_ready is True
    assert result.content_script_state == "ready"
    assert result.worker_state == "running"
    assert result.diagnosis == "ready"
    assert result.url == "https://example.test/path"
    encoded = json.dumps(result.as_dict(), sort_keys=True)
    assert EXTENSION_ID not in encoded
    assert DIGEST_A not in encoded
    assert "token=private" not in encoded
    assert all(session.detached for session in browser.contexts[0].sessions)
    assert browser.closed is True
    methods = [method for session in browser.contexts[0].sessions for method, _ in session.calls]
    assert "Runtime.evaluate" not in methods
    assert "Debugger.getScriptSource" not in methods
    assert "Debugger.enable" in methods
    assert "Target.getTargets" in methods


def test_run_readiness_fails_closed_if_url_changes(monkeypatch) -> None:
    browser = Browser(["wanted"])
    session = browser.contexts[0].sessions[0]
    session.script_events = [_script("content.js")]
    session.worker_targets = [_worker()]
    original_send = session.send

    def send(method, params=None):
        response = original_send(method, params)
        if method == "Target.getTargetInfo" and session.target_info_calls >= 3:
            response["targetInfo"]["url"] = "https://example.test/other"
        return response

    session.send = send
    _install_fake(monkeypatch, browser)
    with pytest.raises(Exception, match="URL changed"):
        playwright_readiness._run_readiness(
            "http://127.0.0.1:9222",
            "wanted",
            ("#ready",),
            (("content.js", DIGEST_A),),
            4,
        )
    assert browser.closed is True


def test_missing_target_disconnects(monkeypatch) -> None:
    browser = Browser(["other"])
    _install_fake(monkeypatch, browser)
    with pytest.raises(Exception, match="not an attached page target"):
        playwright_readiness._run_readiness(
            "http://127.0.0.1:9222",
            "wanted",
            ("#ready",),
            (("content.js", DIGEST_A),),
            4,
        )
    assert browser.contexts[0].sessions[0].detached is True
    assert browser.closed is True


def test_loaders_and_main_input_validation(monkeypatch, capsys) -> None:
    assert playwright_readiness._load_selectors('["#a", "#a", "#b"]') == ("#a", "#b")
    assert playwright_readiness._load_fingerprints(json.dumps([["content.js", DIGEST_A]])) == (
        ("content.js", DIGEST_A),
    )
    with pytest.raises(Exception, match="invalid JSON"):
        playwright_readiness._load_selectors("not-json")
    with pytest.raises(Exception, match="JSON array"):
        playwright_readiness._load_fingerprints("{}")
    with pytest.raises(Exception, match="entry is invalid"):
        playwright_readiness._load_fingerprints('["bad"]')

    monkeypatch.delenv("HOST_OPS_BROWSER_ATTACH_ENDPOINT", raising=False)
    monkeypatch.delenv("HOST_OPS_BROWSER_ATTACH_TARGET_ID", raising=False)
    monkeypatch.delenv(playwright_readiness._SELECTORS_ENV, raising=False)
    monkeypatch.delenv(playwright_readiness._SCRIPT_FINGERPRINTS_ENV, raising=False)
    assert playwright_readiness.main(["--timeout", "3"]) == 2
    assert "missing" in json.loads(capsys.readouterr().out)["error"]


def test_main_handles_timeout_expected_unexpected_and_success(monkeypatch, capsys) -> None:
    monkeypatch.setenv("HOST_OPS_BROWSER_ATTACH_ENDPOINT", "http://127.0.0.1:9222")
    monkeypatch.setenv("HOST_OPS_BROWSER_ATTACH_TARGET_ID", "page-1")
    monkeypatch.setenv(playwright_readiness._SELECTORS_ENV, '["#x"]')
    monkeypatch.setenv(
        playwright_readiness._SCRIPT_FINGERPRINTS_ENV,
        json.dumps([["content.js", DIGEST_A]]),
    )

    assert playwright_readiness.main(["--timeout", "0"]) == 2
    assert "timeout" in json.loads(capsys.readouterr().out)["error"]

    def expected(*_args):
        raise playwright_readiness.BrowserReadinessError("bounded failure")

    monkeypatch.setattr(playwright_readiness, "_run_readiness", expected)
    assert playwright_readiness.main(["--timeout", "3"]) == 1
    assert json.loads(capsys.readouterr().out)["error"] == "bounded failure"

    def unexpected(*_args):
        raise RuntimeError("private detail")

    monkeypatch.setattr(playwright_readiness, "_run_readiness", unexpected)
    assert playwright_readiness.main(["--timeout", "3"]) == 1
    assert json.loads(capsys.readouterr().out)["error"] == "browser readiness inspection failed"

    evidence = SimpleNamespace(as_dict=lambda: {"target_id": "page-1"})
    monkeypatch.setattr(playwright_readiness, "_run_readiness", lambda *_args: evidence)
    assert playwright_readiness.main(["--timeout", "3"]) == 0
    assert json.loads(capsys.readouterr().out)["target_id"] == "page-1"
