from __future__ import annotations

import importlib
import subprocess

import pytest

from local_agent.host_ops.core.execution import DetachedProcessSpawner

process_module = importlib.import_module("local_agent.host_ops.core.execution.process")


def _clear_local_agent_lease_env(monkeypatch) -> None:
    for name in (
        "LOCAL_AGENT_LEASE_FDS",
        "LOCAL_AGENT_RESOURCE_LEASE_FDS",
        "LOCAL_AGENT_LEASE_KEYS_DIGEST",
    ):
        monkeypatch.delenv(name, raising=False)


def test_detached_spawner_owns_detached_process_contract(monkeypatch, tmp_path) -> None:
    _clear_local_agent_lease_env(monkeypatch)
    calls = []

    class FakeProcess:
        pid = 77

    def popen(argv, **kwargs):
        calls.append((tuple(argv), kwargs))
        return FakeProcess()

    monkeypatch.setattr(process_module.subprocess, "Popen", popen)
    assert DetachedProcessSpawner().spawn(["/browser", "about:blank"], cwd=tmp_path) == 77

    argv, kwargs = calls[0]
    assert argv == ("/browser", "about:blank")
    assert kwargs["cwd"] == tmp_path
    assert kwargs["stdin"] is subprocess.DEVNULL
    assert kwargs["stdout"] is subprocess.DEVNULL
    assert kwargs["stderr"] is subprocess.DEVNULL
    assert kwargs["close_fds"] is True
    assert kwargs["start_new_session"] is True
    for name in (
        "LOCAL_AGENT_LEASE_FDS",
        "LOCAL_AGENT_RESOURCE_LEASE_FDS",
        "LOCAL_AGENT_LEASE_KEYS_DIGEST",
    ):
        assert name not in kwargs["env"]


def test_detached_spawner_normalizes_spawn_failure(monkeypatch) -> None:
    _clear_local_agent_lease_env(monkeypatch)

    def fail(*_args, **_kwargs):
        raise OSError("no executable")

    monkeypatch.setattr(process_module.subprocess, "Popen", fail)
    with pytest.raises(RuntimeError, match="detached process spawn failed: OSError"):
        DetachedProcessSpawner().spawn(["/missing"])


def test_detached_spawner_rejects_ambiguous_argv(monkeypatch) -> None:
    _clear_local_agent_lease_env(monkeypatch)
    with pytest.raises(ValueError, match="sequence of argument strings"):
        DetachedProcessSpawner().spawn("/browser")
