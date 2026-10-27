from __future__ import annotations

import importlib

from local_agent.host_ops.capabilities.local.browser import ManagedBrowserSession

cli_main = importlib.import_module("local_agent.host_ops.cli.main")
browser_session_cmd = importlib.import_module("local_agent.host_ops.cli.commands.browser_session")


def test_session_start_parser_exposes_download_directory() -> None:
    args = cli_main.build_parser().parse_args(
        [
            "browser",
            "session",
            "start",
            "--profile-dir",
            "/tmp/profile",
            "--browser-executable",
            "/browser",
            "--download-dir",
            "/tmp/bridge-inbox",
        ]
    )

    assert args.download_dir == "/tmp/bridge-inbox"


def test_run_session_start_forwards_download_directory(monkeypatch) -> None:
    calls = []

    class FakeController:
        def start(
            self,
            profile_dir,
            browser_executable,
            *,
            extension_dirs,
            start_url,
            download_dir,
            timeout_seconds,
        ):
            calls.append(
                (
                    profile_dir,
                    browser_executable,
                    tuple(extension_dirs),
                    start_url,
                    download_dir,
                    timeout_seconds,
                )
            )
            return ManagedBrowserSession(
                state="running",
                pid=4321,
                endpoint="http://127.0.0.1:54321",
                browser="Chrome/153.0.8010.54",
            )

    monkeypatch.setattr(browser_session_cmd, "ManagedBrowserSessionController", FakeController)

    result = browser_session_cmd.run_session_start(
        "/tmp/profile",
        "/browser",
        extension_dirs=["/extension"],
        start_url="https://chatgpt.com/",
        download_dir="/tmp/bridge-inbox",
        timeout_seconds=7,
        as_json=False,
    )

    assert result == 0
    assert calls == [
        (
            "/tmp/profile",
            "/browser",
            ("/extension",),
            "https://chatgpt.com/",
            "/tmp/bridge-inbox",
            7,
        )
    ]
