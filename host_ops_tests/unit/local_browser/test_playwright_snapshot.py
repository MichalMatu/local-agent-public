from __future__ import annotations

import importlib
import json
from types import SimpleNamespace

import pytest

playwright_snapshot = importlib.import_module(
    "local_agent.host_ops.capabilities.local.browser.playwright_snapshot"
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
                    "url": "https://user:pass@example.test/path?token=secret#fragment",
                }
            }
        if method == "Page.getFrameTree":
            return {
                "frameTree": {
                    "frame": {
                        "url": "https://example.test/path?secret=x#f",
                        "mimeType": "text/html",
                    },
                    "childFrames": [{"frame": {"url": "about:blank", "mimeType": "text/html"}}],
                }
            }
        if method == "DOM.getDocument":
            assert params == {"depth": 0, "pierce": False}
            return {"root": {"nodeName": "#document", "childNodeCount": 2}}
        raise AssertionError(method)

    def detach(self):
        self.detached = True


class Context:
    def __init__(self, ids):
        self.pages = [object() for _ in ids]
        self.sessions = [Session(i) for i in ids]

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
        self.calls = []

    def connect_over_cdp(self, endpoint, *, timeout, is_local, no_defaults):
        self.calls.append((endpoint, timeout, is_local, no_defaults))
        return self.browser


class PlaywrightCM:
    def __init__(self, chromium):
        self.value = SimpleNamespace(chromium=chromium)

    def __enter__(self):
        return self.value

    def __exit__(self, *args):
        return None


def test_snapshot_matches_exact_page_target_and_detaches_all_sessions(monkeypatch) -> None:
    browser = Browser(["other", "wanted"])
    chromium = Chromium(browser)
    api = SimpleNamespace(sync_playwright=lambda: PlaywrightCM(chromium))
    monkeypatch.setattr(playwright_snapshot, "_load_playwright_sync_api", lambda: api)

    result = playwright_snapshot._run_snapshot("http://127.0.0.1:9222", "wanted", 4)

    assert chromium.calls == [("http://127.0.0.1:9222", 3000.0, True, True)]
    assert all(session.detached for session in browser.contexts[0].sessions)
    assert browser.closed is True
    assert result.target_id == "wanted"
    assert result.url == "https://example.test/path"
    assert result.main_frame_url == "https://example.test/path"
    assert result.frame_count == 2
    serialized = json.dumps(result.as_dict())
    assert "secret" not in serialized and "pass" not in serialized


def test_snapshot_missing_target_fails_and_disconnects(monkeypatch) -> None:
    browser = Browser(["other"])
    chromium = Chromium(browser)
    api = SimpleNamespace(sync_playwright=lambda: PlaywrightCM(chromium))
    monkeypatch.setattr(playwright_snapshot, "_load_playwright_sync_api", lambda: api)

    with pytest.raises(Exception, match="not an attached page target"):
        playwright_snapshot._run_snapshot("http://127.0.0.1:9222", "wanted", 4)
    assert browser.contexts[0].sessions[0].detached is True
    assert browser.closed is True


def test_protocol_helpers_fail_closed() -> None:
    with pytest.raises(Exception, match="invalid target info"):
        playwright_snapshot._target_info({})
    with pytest.raises(Exception, match="invalid frame data"):
        playwright_snapshot._count_frames({})
    with pytest.raises(Exception, match="invalid root data"):
        playwright_snapshot._document_root({})


def test_main_requires_inputs_and_rejects_bad_timeout(monkeypatch, capsys) -> None:
    monkeypatch.delenv("HOST_OPS_BROWSER_ATTACH_ENDPOINT", raising=False)
    monkeypatch.delenv(playwright_snapshot._SNAPSHOT_TARGET_ENV, raising=False)
    assert playwright_snapshot.main(["--timeout", "3"]) == 2
    assert "missing" in json.loads(capsys.readouterr().out)["error"]

    monkeypatch.setenv("HOST_OPS_BROWSER_ATTACH_ENDPOINT", "http://127.0.0.1:9222")
    monkeypatch.setenv(playwright_snapshot._SNAPSHOT_TARGET_ENV, "page-1")
    assert playwright_snapshot.main(["--timeout", "0.5"]) == 2
    assert "timeout" in json.loads(capsys.readouterr().out)["error"]


def test_optional_playwright_import_error_is_actionable(monkeypatch) -> None:
    def missing(_name):
        raise ModuleNotFoundError("missing", name="playwright")

    monkeypatch.setattr(playwright_snapshot.importlib, "import_module", missing)
    with pytest.raises(Exception, match=r"host-ops\[browser\]"):
        playwright_snapshot._load_playwright_sync_api()


def test_protocol_target_info_validates_id_and_type() -> None:
    with pytest.raises(Exception, match="invalid id"):
        playwright_snapshot._target_info({"targetInfo": {"targetId": "", "type": "page"}})
    with pytest.raises(Exception, match="invalid type"):
        playwright_snapshot._target_info({"targetInfo": {"targetId": "page-1", "type": ""}})


def test_protocol_frame_tree_rejects_invalid_frame_children_and_bound(monkeypatch) -> None:
    with pytest.raises(Exception, match="invalid frame"):
        playwright_snapshot._count_frames({"frameTree": {"frame": None}})
    with pytest.raises(Exception, match="invalid children"):
        playwright_snapshot._count_frames({"frameTree": {"frame": {}, "childFrames": "not-a-list"}})
    monkeypatch.setattr(playwright_snapshot, "_MAX_FRAMES", 1)
    with pytest.raises(Exception, match="exceeded its bound"):
        playwright_snapshot._count_frames(
            {"frameTree": {"frame": {}, "childFrames": [{"frame": {}}]}}
        )


def _protocol_inputs():
    target = {
        "targetId": "page-1",
        "type": "page",
        "title": "Example",
        "url": "https://example.test/",
    }
    frames = {"frameTree": {"frame": {"url": "https://example.test/", "mimeType": "text/html"}}}
    document = {"root": {"nodeName": "#document", "childNodeCount": 1}}
    return target, frames, document


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ("main_url", "main frame has an invalid URL"),
        ("node_name", "invalid node name"),
        ("child_count", "invalid child count"),
        ("target_url", "page target has an invalid URL"),
    ],
)
def test_snapshot_protocol_rejects_invalid_metadata(mutation: str, message: str) -> None:
    target, frames, document = _protocol_inputs()
    if mutation == "main_url":
        frames["frameTree"]["frame"]["url"] = ""
    elif mutation == "node_name":
        document["root"]["nodeName"] = ""
    elif mutation == "child_count":
        document["root"]["childNodeCount"] = True
    else:
        target["url"] = ""
    with pytest.raises(Exception, match=message):
        playwright_snapshot._snapshot_from_protocol(
            "http://127.0.0.1:9222", target, frames, document
        )


def test_operation_timeout_reserves_cleanup_budget() -> None:
    assert playwright_snapshot._operation_timeout_ms(0.5) == 375.0
    assert playwright_snapshot._operation_timeout_ms(10.0) == 9000.0


def test_main_reports_expected_and_unexpected_snapshot_errors(monkeypatch, capsys) -> None:
    monkeypatch.setenv("HOST_OPS_BROWSER_ATTACH_ENDPOINT", "http://127.0.0.1:9222")
    monkeypatch.setenv(playwright_snapshot._SNAPSHOT_TARGET_ENV, "page-1")

    def expected_error(*_args):
        raise playwright_snapshot.BrowserSnapshotError("bounded failure")

    monkeypatch.setattr(playwright_snapshot, "_run_snapshot", expected_error)
    assert playwright_snapshot.main(["--timeout", "3"]) == 1
    assert json.loads(capsys.readouterr().out)["error"] == "bounded failure"

    def unexpected_error(*_args):
        raise RuntimeError("private detail")

    monkeypatch.setattr(playwright_snapshot, "_run_snapshot", unexpected_error)
    assert playwright_snapshot.main(["--timeout", "3"]) == 1
    assert json.loads(capsys.readouterr().out)["error"] == "browser CDP snapshot failed"

    evidence = SimpleNamespace(as_dict=lambda: {"target_id": "page-1"})
    monkeypatch.setattr(playwright_snapshot, "_run_snapshot", lambda *_args: evidence)
    assert playwright_snapshot.main(["--timeout", "3"]) == 0
    assert json.loads(capsys.readouterr().out)["target_id"] == "page-1"


def test_non_playwright_import_error_propagates(monkeypatch) -> None:
    def missing(_name):
        raise ModuleNotFoundError("missing", name="other_dependency")

    monkeypatch.setattr(playwright_snapshot.importlib, "import_module", missing)
    with pytest.raises(ModuleNotFoundError):
        playwright_snapshot._load_playwright_sync_api()
