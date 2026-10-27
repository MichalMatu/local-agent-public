from __future__ import annotations

import importlib
import json

import pytest

from local_agent.host_ops.capabilities.local.browser import (
    BrowserAttachError,
    BrowserAttachInspection,
    BrowserAttachTarget,
)

cli_main = importlib.import_module("local_agent.host_ops.cli.main")
browser_cmd = importlib.import_module("local_agent.host_ops.cli.commands.browser")


def _inspection() -> BrowserAttachInspection:
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


def test_browser_attach_parser_exposes_endpoint_timeout_and_json() -> None:
    args = cli_main.build_parser().parse_args(
        [
            "browser",
            "attach",
            "inspect",
            "--endpoint",
            "http://127.0.0.1:9222",
            "--timeout",
            "9",
            "--json",
        ]
    )

    assert args.command == "browser"
    assert args.browser_command == "attach"
    assert args.browser_attach_command == "inspect"
    assert args.endpoint == "http://127.0.0.1:9222"
    assert args.timeout_seconds == 9.0
    assert args.as_json is True


def test_run_attach_inspect_renders_json(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    calls: list[tuple[str, float]] = []

    class FakeAttacher:
        def inspect(self, endpoint: str, *, timeout_seconds: float):
            calls.append((endpoint, timeout_seconds))
            return _inspection()

    monkeypatch.setattr(browser_cmd, "BrowserCdpAttacher", FakeAttacher)

    assert (
        browser_cmd.run_attach_inspect(
            "http://127.0.0.1:9222",
            timeout_seconds=9.0,
            as_json=True,
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)
    assert calls == [("http://127.0.0.1:9222", 9.0)]
    assert payload["context_count"] == 1
    assert payload["targets"][0]["id"] == "page-1"


def test_run_attach_inspect_reports_failure(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    class FakeAttacher:
        def inspect(self, endpoint: str, *, timeout_seconds: float):
            raise BrowserAttachError("endpoint unavailable")

    monkeypatch.setattr(browser_cmd, "BrowserCdpAttacher", FakeAttacher)

    assert (
        browser_cmd.run_attach_inspect(
            "http://127.0.0.1:9222",
            timeout_seconds=5.0,
            as_json=True,
        )
        == 1
    )
    assert "unavailable" in json.loads(capsys.readouterr().err)["error"]


def test_main_dispatches_browser_attach_inspect(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, object]] = []

    def run_attach_inspect(
        endpoint: str,
        *,
        timeout_seconds: float,
        as_json: bool,
    ) -> int:
        calls.append(
            {
                "endpoint": endpoint,
                "timeout_seconds": timeout_seconds,
                "as_json": as_json,
            }
        )
        return 13

    monkeypatch.setattr(cli_main.browser, "run_attach_inspect", run_attach_inspect)

    result = cli_main.main(
        [
            "browser",
            "attach",
            "inspect",
            "--endpoint",
            "http://127.0.0.1:9222",
            "--timeout",
            "7",
            "--json",
        ]
    )

    assert result == 13
    assert calls == [
        {
            "endpoint": "http://127.0.0.1:9222",
            "timeout_seconds": 7.0,
            "as_json": True,
        }
    ]
