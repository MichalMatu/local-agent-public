from __future__ import annotations

import importlib
import os
import signal
from pathlib import Path

import pytest

from local_agent.host_ops.capabilities.local.browser.models import (
    BrowserInspection,
    BrowserProcess,
    DevtoolsEndpoint,
)

managed_session = importlib.import_module("local_agent.host_ops.capabilities.local.browser.managed_session")


def _executable(tmp_path: Path) -> Path:
    path = tmp_path / "chrome"
    path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    path.chmod(0o755)
    return path


def _process(profile: Path, *, pid: int = 4321, managed: bool = True) -> BrowserProcess:
    return BrowserProcess(
        pid=pid,
        parent_pid=1,
        family="chrome",
        remote_debugging_port=0 if managed else None,
        remote_debugging_address="127.0.0.1" if managed else None,
        remote_debugging_pipe=False,
        user_data_dir=str(profile),
    )


def _endpoint(url: str, *, reachable: bool = True) -> DevtoolsEndpoint:
    return DevtoolsEndpoint(
        endpoint=url,
        reachable=reachable,
        browser="Chrome/153.0.8010.54" if reachable else None,
        protocol_version="1.3" if reachable else None,
        user_agent="ua" if reachable else None,
        web_socket_debugger_url="ws://127.0.0.1/devtools/browser/test" if reachable else None,
        targets=(),
        error=None if reachable else "unreachable",
    )


class FakeInspector:
    def __init__(self, process_provider):
        self._process_provider = process_provider
        self.calls = []

    def inspect(self, *, endpoints=(), timeout_seconds=5.0, limits=None):
        self.calls.append((tuple(endpoints), timeout_seconds, limits))
        endpoint_items = tuple(_endpoint(endpoint) for endpoint in endpoints)
        process = self._process_provider()
        return BrowserInspection(
            processes=() if process is None else (process,),
            endpoints=endpoint_items,
            warnings=(),
        )


def test_start_creates_owned_profile_and_returns_dynamic_loopback_endpoint(tmp_path: Path) -> None:
    profile = tmp_path / "bridge-profile"
    executable = _executable(tmp_path)
    extension = tmp_path / "extension"
    extension.mkdir()
    launched = {"value": False}
    argv_seen: list[str] = []

    def process_provider():
        return _process(profile) if launched["value"] else None

    def launcher(argv):
        argv_seen.extend(argv)
        profile.mkdir(exist_ok=True)
        (profile / "DevToolsActivePort").write_text(
            "54321\n/devtools/browser/test\n", encoding="utf-8"
        )
        launched["value"] = True
        return 4321

    controller = managed_session.ManagedBrowserSessionController(
        FakeInspector(process_provider),
        launcher=launcher,
        pid_probe=lambda pid: pid == 4321,
        sleeper=lambda _seconds: None,
    )

    result = controller.start(
        str(profile),
        str(executable),
        extension_dirs=[str(extension)],
        start_url="https://chatgpt.com/",
        timeout_seconds=2,
    )

    assert result.as_dict() == {
        "state": "running",
        "pid": 4321,
        "endpoint": "http://127.0.0.1:54321",
        "browser": "Chrome/153.0.8010.54",
    }
    assert (profile / ".hostops-managed-cdp-profile.json").exists()
    assert f"--user-data-dir={profile}" in argv_seen
    assert "--remote-debugging-address=127.0.0.1" in argv_seen
    assert "--remote-debugging-port=0" in argv_seen
    assert f"--load-extension={extension}" in argv_seen
    assert f"--disable-extensions-except={extension}" in argv_seen
    assert argv_seen[-1] == "https://chatgpt.com/"


def test_start_rejects_nonempty_unowned_profile(tmp_path: Path) -> None:
    profile = tmp_path / "existing-profile"
    profile.mkdir()
    (profile / "Cookies").write_text("private", encoding="utf-8")
    executable = _executable(tmp_path)

    controller = managed_session.ManagedBrowserSessionController(
        FakeInspector(lambda: None),
        launcher=lambda _argv: 1,
    )
    with pytest.raises(
        managed_session.ManagedBrowserSessionError,
        match=r"not empty.*no host-ops ownership marker",
    ):
        controller.start(str(profile), str(executable))


def test_profile_path_must_be_absolute_and_whitespace_free(tmp_path: Path) -> None:
    controller = managed_session.ManagedBrowserSessionController(
        FakeInspector(lambda: None), launcher=lambda _argv: 1
    )
    executable = _executable(tmp_path)

    with pytest.raises(ValueError, match="absolute"):
        controller.start("relative-profile", str(executable))
    with pytest.raises(ValueError, match="whitespace"):
        controller.start(str(tmp_path / "profile with space"), str(executable))


@pytest.mark.parametrize(
    "url, message",
    [
        ("ftp://example.test/", "http or https"),
        ("https://user:secret@example.test/", "credentials"),
        ("https://example.test/?token=secret", "query or fragment"),
        ("https://example.test/#state", "query or fragment"),
    ],
)
def test_start_rejects_unsafe_start_urls(tmp_path: Path, url: str, message: str) -> None:
    executable = _executable(tmp_path)
    controller = managed_session.ManagedBrowserSessionController(
        FakeInspector(lambda: None), launcher=lambda _argv: 1
    )
    with pytest.raises(ValueError, match=message):
        controller.start(str(tmp_path / "profile"), str(executable), start_url=url)


def test_status_is_stopped_for_missing_profile_without_process_scan(tmp_path: Path) -> None:
    class FailingInspector:
        def inspect(self, **_kwargs):
            raise AssertionError("process scan should not run")

    result = managed_session.ManagedBrowserSessionController(FailingInspector()).status(
        str(tmp_path / "missing")
    )
    assert result.state == "stopped"
    assert result.pid is None


def test_status_fails_closed_when_profile_is_used_outside_managed_cdp_contract(
    tmp_path: Path,
) -> None:
    profile = managed_session._initialize_managed_profile(str(tmp_path / "profile"))
    inspector = FakeInspector(lambda: _process(profile, managed=False))
    controller = managed_session.ManagedBrowserSessionController(inspector)

    with pytest.raises(
        managed_session.ManagedBrowserSessionError,
        match="outside the dynamic-port CDP contract",
    ):
        controller.status(str(profile))


def test_status_reports_unhealthy_when_active_port_is_missing(tmp_path: Path) -> None:
    profile = managed_session._initialize_managed_profile(str(tmp_path / "profile"))
    controller = managed_session.ManagedBrowserSessionController(
        FakeInspector(lambda: _process(profile))
    )

    result = controller.status(str(profile))

    assert result.as_dict() == {
        "state": "unhealthy",
        "pid": 4321,
        "endpoint": None,
        "browser": None,
    }


def test_stop_sends_only_sigterm_to_exact_managed_process(tmp_path: Path) -> None:
    profile = managed_session._initialize_managed_profile(str(tmp_path / "profile"))
    active = {"value": True}
    signals: list[tuple[int, int]] = []

    def process_provider():
        return _process(profile) if active["value"] else None

    def signal_sender(pid: int, sig: int) -> None:
        signals.append((pid, sig))
        active["value"] = False

    controller = managed_session.ManagedBrowserSessionController(
        FakeInspector(process_provider),
        signal_sender=signal_sender,
        sleeper=lambda _seconds: None,
    )

    result = controller.stop(str(profile), timeout_seconds=2)

    assert result.state == "stopped"
    assert signals == [(4321, signal.SIGTERM)]
    assert signal.SIGKILL not in [sig for _, sig in signals]


def test_stop_refuses_to_signal_unmanaged_profile_process(tmp_path: Path) -> None:
    profile = managed_session._initialize_managed_profile(str(tmp_path / "profile"))
    signals: list[tuple[int, int]] = []
    controller = managed_session.ManagedBrowserSessionController(
        FakeInspector(lambda: _process(profile, managed=False)),
        signal_sender=lambda pid, sig: signals.append((pid, sig)),
    )

    with pytest.raises(managed_session.ManagedBrowserSessionError):
        controller.stop(str(profile), timeout_seconds=1)
    assert signals == []


def test_extension_dirs_are_bounded_deduplicated_and_require_directories(tmp_path: Path) -> None:
    extension = tmp_path / "extension"
    extension.mkdir()
    assert managed_session._validate_extension_dirs([str(extension), str(extension)]) == (
        str(extension.resolve()),
    )
    with pytest.raises(ValueError, match="existing directory"):
        managed_session._validate_extension_dirs([str(tmp_path / "missing")])
    with pytest.raises(ValueError, match="at most"):
        managed_session._validate_extension_dirs([str(extension)] * 9)


def test_default_launcher_uses_core_execution_spawner(monkeypatch) -> None:
    calls = []

    class FakeSpawner:
        def spawn(self, argv):
            calls.append(tuple(argv))
            return 77

    monkeypatch.setattr(managed_session, "DetachedProcessSpawner", FakeSpawner)
    assert managed_session._launch_browser(["/browser", "about:blank"]) == 77
    assert calls == [("/browser", "about:blank")]


def test_default_launcher_normalizes_core_spawn_failure(monkeypatch) -> None:
    class FailedSpawner:
        def spawn(self, _argv):
            raise RuntimeError("spawn refused")

    monkeypatch.setattr(managed_session, "DetachedProcessSpawner", FailedSpawner)
    with pytest.raises(managed_session.ManagedBrowserSessionError, match="spawn refused"):
        managed_session._launch_browser(["/browser", "about:blank"])


def test_pid_probe_treats_permission_error_as_existing(monkeypatch) -> None:
    def denied(_pid: int, _sig: int) -> None:
        raise PermissionError

    monkeypatch.setattr(os, "kill", denied)
    assert managed_session._pid_exists(123) is True
