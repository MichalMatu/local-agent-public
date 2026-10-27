from __future__ import annotations

import importlib
import signal
from pathlib import Path

import pytest

from local_agent.host_ops.capabilities.local.browser.models import BrowserInspection, BrowserProcess

interactive = importlib.import_module("local_agent.host_ops.capabilities.local.browser.interactive_session")
managed = importlib.import_module("local_agent.host_ops.capabilities.local.browser.managed_session")


def _executable(tmp_path: Path) -> Path:
    path = tmp_path / "chrome"
    path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    path.chmod(0o755)
    return path


def _process(profile: Path, *, pid: int = 4321, interactive_mode: bool = True) -> BrowserProcess:
    return BrowserProcess(
        pid=pid,
        parent_pid=1,
        family="chrome",
        remote_debugging_port=None if interactive_mode else 0,
        remote_debugging_address=None if interactive_mode else "127.0.0.1",
        remote_debugging_pipe=False,
        user_data_dir=str(profile),
    )


class FakeInspector:
    def __init__(self, provider):
        self._provider = provider

    def inspect(self, **_kwargs):
        value = self._provider()
        if value is None:
            processes = ()
        elif isinstance(value, tuple):
            processes = value
        else:
            processes = (value,)
        return BrowserInspection(processes=processes, endpoints=(), warnings=())


def _owned_profile(tmp_path: Path) -> Path:
    return managed._initialize_managed_profile(str(tmp_path / "profile"))


def test_start_uses_owned_profile_without_cdp_or_extension_flags(tmp_path: Path) -> None:
    profile = _owned_profile(tmp_path)
    executable = _executable(tmp_path)
    active = {"value": False}
    argv_seen: list[str] = []

    def launcher(argv):
        argv_seen.extend(argv)
        active["value"] = True
        return 4321

    controller = interactive.InteractiveBrowserSessionController(
        FakeInspector(lambda: _process(profile) if active["value"] else None),
        launcher=launcher,
        pid_probe=lambda pid: pid == 4321,
        sleeper=lambda _seconds: None,
    )
    result = controller.start(
        str(profile),
        str(executable),
        start_url="https://chatgpt.com/",
        timeout_seconds=2,
    )

    assert result.state == "running"
    assert result.pid == 4321
    assert result.endpoint is None
    assert f"--user-data-dir={profile}" in argv_seen
    assert not any(arg.startswith("--remote-debugging-") for arg in argv_seen)
    assert not any("load-extension" in arg for arg in argv_seen)
    assert not any("disable-extensions-except" in arg for arg in argv_seen)
    assert argv_seen[-1] == "https://chatgpt.com/"


def test_interactive_mode_requires_existing_owned_profile(tmp_path: Path) -> None:
    executable = _executable(tmp_path)
    controller = interactive.InteractiveBrowserSessionController(FakeInspector(lambda: None))
    with pytest.raises(
        interactive.InteractiveBrowserSessionError, match="existing host-ops managed"
    ):
        controller.start(str(tmp_path / "missing"), str(executable))

    unowned = tmp_path / "unowned"
    unowned.mkdir()
    with pytest.raises(interactive.InteractiveBrowserSessionError, match="not owned"):
        controller.status(str(unowned))


def test_status_rejects_managed_cdp_process(tmp_path: Path) -> None:
    profile = _owned_profile(tmp_path)
    controller = interactive.InteractiveBrowserSessionController(
        FakeInspector(lambda: _process(profile, interactive_mode=False))
    )
    with pytest.raises(interactive.InteractiveBrowserSessionError, match="no-CDP contract"):
        controller.status(str(profile))


def test_status_refuses_multiple_profile_roots(tmp_path: Path) -> None:
    profile = _owned_profile(tmp_path)
    controller = interactive.InteractiveBrowserSessionController(
        FakeInspector(lambda: (_process(profile, pid=1), _process(profile, pid=2)))
    )
    with pytest.raises(interactive.InteractiveBrowserSessionError, match="multiple browser root"):
        controller.status(str(profile))


def test_stop_sends_only_sigterm_and_clears_only_restore_state(tmp_path: Path) -> None:
    profile = _owned_profile(tmp_path)
    default = profile / "Default"
    sessions = default / "Sessions"
    sessions.mkdir(parents=True)
    (sessions / "Session_1").write_text("tabs", encoding="utf-8")
    (sessions / "Tabs_1").write_text("tabs", encoding="utf-8")
    (default / "Last Session").write_text("legacy", encoding="utf-8")
    cookies = default / "Cookies"
    cookies.write_text("keep-me", encoding="utf-8")
    active = {"value": True}
    signals: list[tuple[int, int]] = []

    def sender(pid: int, sig: int) -> None:
        signals.append((pid, sig))
        active["value"] = False

    controller = interactive.InteractiveBrowserSessionController(
        FakeInspector(lambda: _process(profile) if active["value"] else None),
        signal_sender=sender,
        sleeper=lambda _seconds: None,
    )
    result = controller.stop(str(profile), clear_session_restore=True, timeout_seconds=2)

    assert result.state == "stopped"
    assert signals == [(4321, signal.SIGTERM)]
    assert cookies.read_text(encoding="utf-8") == "keep-me"
    assert list(sessions.iterdir()) == []
    assert not (default / "Last Session").exists()


def test_restore_cleanup_fails_closed_on_symlink(tmp_path: Path) -> None:
    profile = _owned_profile(tmp_path)
    default = profile / "Default"
    default.mkdir()
    outside = tmp_path / "outside"
    outside.write_text("private", encoding="utf-8")
    (default / "Last Tabs").symlink_to(outside)

    controller = interactive.InteractiveBrowserSessionController(FakeInspector(lambda: None))
    with pytest.raises(interactive.InteractiveBrowserSessionError, match="regular file"):
        controller.stop(str(profile), clear_session_restore=True)
    assert outside.read_text(encoding="utf-8") == "private"


def test_stop_refuses_to_signal_managed_cdp_process(tmp_path: Path) -> None:
    profile = _owned_profile(tmp_path)
    signals: list[tuple[int, int]] = []
    controller = interactive.InteractiveBrowserSessionController(
        FakeInspector(lambda: _process(profile, interactive_mode=False)),
        signal_sender=lambda pid, sig: signals.append((pid, sig)),
    )
    with pytest.raises(interactive.InteractiveBrowserSessionError):
        controller.stop(str(profile))
    assert signals == []
