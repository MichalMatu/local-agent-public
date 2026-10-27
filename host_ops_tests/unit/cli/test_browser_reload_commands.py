from __future__ import annotations

import importlib
import json

from local_agent.host_ops.capabilities.local.browser import BrowserReloadError, BrowserReloadResult

cli_main = importlib.import_module("local_agent.host_ops.cli.main")
browser_cmd = importlib.import_module("local_agent.host_ops.cli.commands.browser")


def _reload_result() -> BrowserReloadResult:
    return BrowserReloadResult(
        endpoint="http://127.0.0.1:9222",
        target_id="page-1",
        target_type="page",
        expected_url="https://example.test/path",
        before_url="https://example.test/path",
        before_title="Before",
        after_url="https://example.test/path",
        after_title="After",
    )


def test_reload_parser_requires_expected_url_and_exposes_timeout_json() -> None:
    args = cli_main.build_parser().parse_args(
        [
            "browser",
            "attach",
            "reload",
            "--endpoint",
            "http://127.0.0.1:9222",
            "--target-id",
            "page-1",
            "--expect-url",
            "https://example.test/path",
            "--timeout",
            "9",
            "--json",
        ]
    )
    assert args.browser_command == "attach"
    assert args.browser_attach_command == "reload"
    assert args.endpoint == "http://127.0.0.1:9222"
    assert args.target_id == "page-1"
    assert args.expected_url == "https://example.test/path"
    assert args.timeout_seconds == 9
    assert args.as_json is True


def test_run_reload_renders_json_and_failure(monkeypatch, capsys) -> None:
    class FakeReloader:
        def reload(self, endpoint, target_id, expected_url, *, timeout_seconds):
            return _reload_result()

    monkeypatch.setattr(browser_cmd, "BrowserCdpReloader", FakeReloader)
    assert (
        browser_cmd.run_attach_reload(
            "http://127.0.0.1:9222",
            "page-1",
            "https://example.test/path",
            timeout_seconds=9,
            as_json=True,
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["after_title"] == "After"

    class FailedReloader:
        def reload(self, endpoint, target_id, expected_url, *, timeout_seconds):
            raise BrowserReloadError("guard failed")

    monkeypatch.setattr(browser_cmd, "BrowserCdpReloader", FailedReloader)
    assert (
        browser_cmd.run_attach_reload(
            "http://127.0.0.1:9222",
            "page-1",
            "https://example.test/path",
            timeout_seconds=9,
            as_json=True,
        )
        == 1
    )
    assert "guard failed" in json.loads(capsys.readouterr().err)["error"]


def test_main_dispatches_guarded_reload(monkeypatch) -> None:
    calls = []

    def run(endpoint, target_id, expected_url, *, timeout_seconds, as_json):
        calls.append((endpoint, target_id, expected_url, timeout_seconds, as_json))
        return 17

    monkeypatch.setattr(browser_cmd, "run_attach_reload", run)
    assert (
        cli_main.main(
            [
                "browser",
                "attach",
                "reload",
                "--endpoint",
                "http://127.0.0.1:9222",
                "--target-id",
                "page-1",
                "--expect-url",
                "https://example.test/path",
                "--json",
            ]
        )
        == 17
    )
    assert calls == [
        (
            "http://127.0.0.1:9222",
            "page-1",
            "https://example.test/path",
            15.0,
            True,
        )
    ]
