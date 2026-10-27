from __future__ import annotations

import importlib
import json

import pytest

from local_agent.host_ops.capabilities.local.browser import (
    BrowserInspection,
    BrowserInspectionError,
    BrowserProcess,
    DevtoolsEndpoint,
    DevtoolsTarget,
)

cli_main = importlib.import_module("local_agent.host_ops.cli.main")
browser_cmd = importlib.import_module("local_agent.host_ops.cli.commands.browser")


def _inspection() -> BrowserInspection:
    return BrowserInspection(
        processes=(
            BrowserProcess(
                pid=101,
                parent_pid=1,
                family="chrome",
                remote_debugging_port=9222,
                remote_debugging_address=None,
                remote_debugging_pipe=False,
                user_data_dir=None,
            ),
        ),
        endpoints=(
            DevtoolsEndpoint(
                endpoint="http://127.0.0.1:9222",
                reachable=True,
                browser="Chrome/153.0",
                protocol_version="1.3",
                user_agent="test-agent",
                web_socket_debugger_url="ws://127.0.0.1:9222/devtools/browser/root",
                targets=(
                    DevtoolsTarget(
                        target_id="page-1",
                        target_type="page",
                        title="ChatGPT",
                        url="https://chatgpt.com/c/abc",
                        web_socket_debugger_url=("ws://127.0.0.1:9222/devtools/page/page-1"),
                    ),
                ),
            ),
        ),
        warnings=(),
    )


def test_browser_inspect_parser_exposes_endpoints_timeout_and_json() -> None:
    args = cli_main.build_parser().parse_args(
        [
            "browser",
            "inspect",
            "--endpoint",
            "http://127.0.0.1:9222",
            "--endpoint",
            "http://localhost:9333",
            "--timeout",
            "3",
            "--json",
        ]
    )

    assert args.command == "browser"
    assert args.browser_command == "inspect"
    assert args.endpoints == ["http://127.0.0.1:9222", "http://localhost:9333"]
    assert args.timeout_seconds == 3.0
    assert args.as_json is True


def test_run_inspect_renders_json(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    class FakeInspector:
        def inspect(self, *, endpoints, timeout_seconds, limits):
            assert endpoints == ["http://127.0.0.1:9222"]
            assert timeout_seconds == 4.0
            assert limits.timeout_seconds == 4.0
            return _inspection()

    monkeypatch.setattr(browser_cmd, "BrowserInspector", FakeInspector)

    assert (
        browser_cmd.run_inspect(
            endpoints=["http://127.0.0.1:9222"],
            timeout_seconds=4.0,
            as_json=True,
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["processes"][0]["family"] == "chrome"
    assert "command" not in payload["processes"][0]
    assert payload["endpoints"][0]["targets"][0]["url"] == "https://chatgpt.com/c/abc"


def test_run_inspect_reports_failure(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    class FakeInspector:
        def inspect(self, *, endpoints, timeout_seconds, limits):
            raise BrowserInspectionError("process listing unavailable")

    monkeypatch.setattr(browser_cmd, "BrowserInspector", FakeInspector)

    assert browser_cmd.run_inspect(endpoints=[], timeout_seconds=4.0, as_json=True) == 1
    assert "unavailable" in json.loads(capsys.readouterr().err)["error"]


def test_main_dispatches_browser_inspect(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, object]] = []

    def run_inspect(*, endpoints, timeout_seconds, as_json):
        calls.append(
            {
                "endpoints": endpoints,
                "timeout_seconds": timeout_seconds,
                "as_json": as_json,
            }
        )
        return 7

    monkeypatch.setattr(cli_main.browser, "run_inspect", run_inspect)

    result = cli_main.main(
        [
            "browser",
            "inspect",
            "--endpoint",
            "http://127.0.0.1:9222",
            "--timeout",
            "2",
            "--json",
        ]
    )

    assert result == 7
    assert calls == [
        {
            "endpoints": ["http://127.0.0.1:9222"],
            "timeout_seconds": 2.0,
            "as_json": True,
        }
    ]
