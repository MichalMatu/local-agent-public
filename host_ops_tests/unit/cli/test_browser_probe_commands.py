from __future__ import annotations

import importlib
import json

import pytest

from local_agent.host_ops.capabilities.local.browser import ManagedBrowserError, ManagedBrowserProbe

cli_main = importlib.import_module("local_agent.host_ops.cli.main")
browser_cmd = importlib.import_module("local_agent.host_ops.cli.commands.browser")


def _probe() -> ManagedBrowserProbe:
    return ManagedBrowserProbe(
        engine="firefox",
        requested_url="https://example.test/start",
        final_url="https://example.test/final",
        title="Probe title",
        status_code=204,
        console_error_count=1,
        page_error_count=0,
        request_failure_count=2,
    )


def test_browser_probe_parser_exposes_url_engine_timeout_and_json() -> None:
    args = cli_main.build_parser().parse_args(
        [
            "browser",
            "probe",
            "https://example.test/path?secret=value",
            "--engine",
            "firefox",
            "--timeout",
            "9",
            "--json",
        ]
    )

    assert args.command == "browser"
    assert args.browser_command == "probe"
    assert args.url == "https://example.test/path?secret=value"
    assert args.engine == "firefox"
    assert args.timeout_seconds == 9.0
    assert args.as_json is True


def test_run_probe_renders_json(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    calls: list[tuple[str, str, float]] = []

    class FakeProber:
        def probe(self, url: str, *, engine: str, timeout_seconds: float):
            calls.append((url, engine, timeout_seconds))
            return _probe()

    monkeypatch.setattr(browser_cmd, "ManagedBrowserProber", FakeProber)

    assert (
        browser_cmd.run_probe(
            "https://example.test/path?secret=value",
            engine="firefox",
            timeout_seconds=9.0,
            as_json=True,
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)
    assert calls == [("https://example.test/path?secret=value", "firefox", 9.0)]
    assert payload["final_url"] == "https://example.test/final"
    assert payload["request_failure_count"] == 2


def test_run_probe_reports_failure(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    class FakeProber:
        def probe(self, url: str, *, engine: str, timeout_seconds: float):
            raise ManagedBrowserError("Playwright is not installed")

    monkeypatch.setattr(browser_cmd, "ManagedBrowserProber", FakeProber)

    assert (
        browser_cmd.run_probe(
            "https://example.test/",
            engine="chromium",
            timeout_seconds=5.0,
            as_json=True,
        )
        == 1
    )
    assert "not installed" in json.loads(capsys.readouterr().err)["error"]


def test_main_dispatches_browser_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, object]] = []

    def run_probe(url: str, *, engine: str, timeout_seconds: float, as_json: bool) -> int:
        calls.append(
            {
                "url": url,
                "engine": engine,
                "timeout_seconds": timeout_seconds,
                "as_json": as_json,
            }
        )
        return 11

    monkeypatch.setattr(cli_main.browser, "run_probe", run_probe)

    result = cli_main.main(
        [
            "browser",
            "probe",
            "https://example.test/path",
            "--engine",
            "webkit",
            "--timeout",
            "7",
            "--json",
        ]
    )

    assert result == 11
    assert calls == [
        {
            "url": "https://example.test/path",
            "engine": "webkit",
            "timeout_seconds": 7.0,
            "as_json": True,
        }
    ]
