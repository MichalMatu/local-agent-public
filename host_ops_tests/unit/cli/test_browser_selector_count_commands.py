from __future__ import annotations

import importlib
import json

from local_agent.host_ops.capabilities.local.browser import (
    BrowserSelectorCount,
    BrowserSelectorCountError,
    BrowserSelectorInspection,
)

cli_main = importlib.import_module("local_agent.host_ops.cli.main")
browser_cmd = importlib.import_module("local_agent.host_ops.cli.commands.browser")


def _result() -> BrowserSelectorInspection:
    return BrowserSelectorInspection(
        endpoint="http://127.0.0.1:9222",
        target_id="page-1",
        target_type="page",
        title="Example",
        url="https://example.test/",
        selectors=(BrowserSelectorCount("#prompt-textarea", 1),),
    )


def test_parser_exposes_repeated_selectors() -> None:
    args = cli_main.build_parser().parse_args(
        [
            "browser",
            "attach",
            "selectors",
            "--endpoint",
            "http://127.0.0.1:9222",
            "--target-id",
            "page-1",
            "--selector",
            "#prompt-textarea",
            "--selector",
            "#composer-submit-button",
            "--json",
        ]
    )
    assert args.browser_attach_command == "selectors"
    assert args.selectors == ["#prompt-textarea", "#composer-submit-button"]


def test_run_selectors_renders_json_and_failure(monkeypatch, capsys) -> None:
    class FakeCounter:
        def count(self, endpoint, target_id, selectors, *, timeout_seconds):
            return _result()

    monkeypatch.setattr(browser_cmd, "BrowserCdpSelectorCounter", FakeCounter)
    assert (
        browser_cmd.run_attach_selectors(
            "http://127.0.0.1:9222",
            "page-1",
            ["#prompt-textarea"],
            timeout_seconds=9,
            as_json=True,
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["selectors"][0]["match_count"] == 1

    class BadCounter:
        def count(self, endpoint, target_id, selectors, *, timeout_seconds):
            raise BrowserSelectorCountError("bad selector count")

    monkeypatch.setattr(browser_cmd, "BrowserCdpSelectorCounter", BadCounter)
    assert (
        browser_cmd.run_attach_selectors(
            "http://127.0.0.1:9222",
            "page-1",
            ["#prompt-textarea"],
            timeout_seconds=9,
            as_json=True,
        )
        == 1
    )
    assert "bad selector count" in json.loads(capsys.readouterr().err)["error"]
