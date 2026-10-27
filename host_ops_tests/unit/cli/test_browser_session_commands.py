from __future__ import annotations

import importlib
import json

from local_agent.host_ops.capabilities.local.browser import (
    InteractiveBrowserSessionError,
    ManagedBrowserSession,
    ManagedBrowserSessionError,
)

cli_main = importlib.import_module("local_agent.host_ops.cli.main")
browser_cmd = importlib.import_module("local_agent.host_ops.cli.commands.browser")
browser_session_cmd = importlib.import_module("local_agent.host_ops.cli.commands.browser_session")


def _running() -> ManagedBrowserSession:
    return ManagedBrowserSession(
        state="running",
        pid=4321,
        endpoint="http://127.0.0.1:54321",
        browser="Chrome/153.0.8010.54",
    )


def _interactive_running() -> ManagedBrowserSession:
    return ManagedBrowserSession(
        state="running",
        pid=4321,
        endpoint=None,
        browser="chrome",
    )


def test_session_start_parser_exposes_explicit_profile_browser_extensions_url_and_timeout() -> None:
    args = cli_main.build_parser().parse_args(
        [
            "browser",
            "session",
            "start",
            "--profile-dir",
            "/tmp/bridge-profile",
            "--browser-executable",
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            "--extension-dir",
            "/tmp/extension-a",
            "--extension-dir",
            "/tmp/extension-b",
            "--url",
            "https://chatgpt.com/",
            "--timeout",
            "12",
            "--json",
        ]
    )
    assert args.browser_command == "session"
    assert args.browser_session_command == "start"
    assert args.profile_dir == "/tmp/bridge-profile"
    assert args.browser_executable.endswith("/Google Chrome")
    assert args.extension_dirs == ["/tmp/extension-a", "/tmp/extension-b"]
    assert args.start_url == "https://chatgpt.com/"
    assert args.timeout_seconds == 12
    assert args.as_json is True


def test_session_status_and_stop_parsers_are_profile_scoped() -> None:
    for command, default_timeout in (("status", 5.0), ("stop", 10.0)):
        args = cli_main.build_parser().parse_args(
            ["browser", "session", command, "--profile-dir", "/tmp/profile", "--json"]
        )
        assert args.browser_command == "session"
        assert args.browser_session_command == command
        assert args.profile_dir == "/tmp/profile"
        assert args.timeout_seconds == default_timeout
        assert args.as_json is True


def test_interactive_parsers_expose_exact_profile_and_cleanup_flag() -> None:
    start = cli_main.build_parser().parse_args(
        [
            "browser",
            "session",
            "interactive-start",
            "--profile-dir",
            "/tmp/profile",
            "--browser-executable",
            "/browser",
            "--url",
            "https://chatgpt.com/",
            "--json",
        ]
    )
    assert start.browser_session_command == "interactive-start"
    assert start.profile_dir == "/tmp/profile"
    assert start.browser_executable == "/browser"
    assert start.start_url == "https://chatgpt.com/"

    status = cli_main.build_parser().parse_args(
        ["browser", "session", "interactive-status", "--profile-dir", "/tmp/profile", "--json"]
    )
    assert status.browser_session_command == "interactive-status"
    assert status.timeout_seconds == 5.0

    stop = cli_main.build_parser().parse_args(
        [
            "browser",
            "session",
            "interactive-stop",
            "--profile-dir",
            "/tmp/profile",
            "--clear-session-restore",
            "--json",
        ]
    )
    assert stop.browser_session_command == "interactive-stop"
    assert stop.clear_session_restore is True
    assert stop.timeout_seconds == 10.0


def test_run_session_start_renders_json_and_failure(monkeypatch, capsys) -> None:
    class FakeController:
        def start(
            self,
            profile_dir,
            browser_executable,
            *,
            extension_dirs,
            start_url,
            timeout_seconds,
        ):
            assert profile_dir == "/tmp/profile"
            assert browser_executable == "/browser"
            assert extension_dirs == ["/extension"]
            assert start_url == "https://chatgpt.com/"
            assert timeout_seconds == 7
            return _running()

    monkeypatch.setattr(browser_session_cmd, "ManagedBrowserSessionController", FakeController)
    assert (
        browser_session_cmd.run_session_start(
            "/tmp/profile",
            "/browser",
            extension_dirs=["/extension"],
            start_url="https://chatgpt.com/",
            timeout_seconds=7,
            as_json=True,
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["endpoint"] == "http://127.0.0.1:54321"

    class FailedController:
        def start(self, *_args, **_kwargs):
            raise ManagedBrowserSessionError("profile guard failed")

    monkeypatch.setattr(browser_session_cmd, "ManagedBrowserSessionController", FailedController)
    assert (
        browser_session_cmd.run_session_start(
            "/tmp/profile",
            "/browser",
            extension_dirs=[],
            start_url=None,
            timeout_seconds=7,
            as_json=True,
        )
        == 1
    )
    assert "profile guard failed" in json.loads(capsys.readouterr().err)["error"]


def test_run_session_status_and_stop_dispatch_controller(monkeypatch, capsys) -> None:
    calls = []

    class FakeController:
        def status(self, profile_dir, *, timeout_seconds):
            calls.append(("status", profile_dir, timeout_seconds))
            return _running()

        def stop(self, profile_dir, *, timeout_seconds):
            calls.append(("stop", profile_dir, timeout_seconds))
            return ManagedBrowserSession(state="stopped", pid=None, endpoint=None, browser=None)

    monkeypatch.setattr(browser_session_cmd, "ManagedBrowserSessionController", FakeController)
    assert (
        browser_session_cmd.run_session_status("/tmp/profile", timeout_seconds=4, as_json=True) == 0
    )
    assert json.loads(capsys.readouterr().out)["state"] == "running"
    assert (
        browser_session_cmd.run_session_stop("/tmp/profile", timeout_seconds=6, as_json=True) == 0
    )
    assert json.loads(capsys.readouterr().out)["state"] == "stopped"
    assert calls == [("status", "/tmp/profile", 4), ("stop", "/tmp/profile", 6)]


def test_run_interactive_lifecycle_dispatches_controller(monkeypatch, capsys) -> None:
    calls = []

    class FakeController:
        def start(self, profile_dir, browser_executable, *, start_url, timeout_seconds):
            calls.append(("start", profile_dir, browser_executable, start_url, timeout_seconds))
            return _interactive_running()

        def status(self, profile_dir, *, timeout_seconds):
            calls.append(("status", profile_dir, timeout_seconds))
            return _interactive_running()

        def stop(self, profile_dir, *, clear_session_restore, timeout_seconds):
            calls.append(("stop", profile_dir, clear_session_restore, timeout_seconds))
            return ManagedBrowserSession(state="stopped", pid=None, endpoint=None, browser=None)

    monkeypatch.setattr(browser_session_cmd, "InteractiveBrowserSessionController", FakeController)
    assert (
        browser_session_cmd.run_interactive_start(
            "/tmp/profile",
            "/browser",
            start_url="https://chatgpt.com/",
            timeout_seconds=7,
            as_json=True,
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["endpoint"] is None
    assert (
        browser_session_cmd.run_interactive_status("/tmp/profile", timeout_seconds=4, as_json=True)
        == 0
    )
    capsys.readouterr()
    assert (
        browser_session_cmd.run_interactive_stop(
            "/tmp/profile",
            clear_session_restore=True,
            timeout_seconds=6,
            as_json=True,
        )
        == 0
    )
    capsys.readouterr()
    assert calls == [
        ("start", "/tmp/profile", "/browser", "https://chatgpt.com/", 7),
        ("status", "/tmp/profile", 4),
        ("stop", "/tmp/profile", True, 6),
    ]


def test_run_interactive_failure_is_rendered(monkeypatch, capsys) -> None:
    class FailedController:
        def status(self, *_args, **_kwargs):
            raise InteractiveBrowserSessionError("wrong process mode")

    monkeypatch.setattr(
        browser_session_cmd, "InteractiveBrowserSessionController", FailedController
    )
    assert (
        browser_session_cmd.run_interactive_status("/tmp/profile", timeout_seconds=4, as_json=True)
        == 1
    )
    assert "wrong process mode" in json.loads(capsys.readouterr().err)["error"]


def test_main_dispatches_session_start(monkeypatch) -> None:
    calls = []

    def run(args):
        calls.append(
            (
                args.profile_dir,
                args.browser_executable,
                args.extension_dirs,
                args.start_url,
                args.timeout_seconds,
                args.as_json,
            )
        )
        return 19

    monkeypatch.setattr(browser_cmd, "run_browser_session", run)
    assert (
        cli_main.main(
            [
                "browser",
                "session",
                "start",
                "--profile-dir",
                "/tmp/profile",
                "--browser-executable",
                "/browser",
                "--extension-dir",
                "/extension",
                "--url",
                "https://chatgpt.com/",
                "--json",
            ]
        )
        == 19
    )
    assert calls == [
        (
            "/tmp/profile",
            "/browser",
            ["/extension"],
            "https://chatgpt.com/",
            15.0,
            True,
        )
    ]
