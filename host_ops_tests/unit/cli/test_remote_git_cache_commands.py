from __future__ import annotations

import importlib
import json

import pytest

from local_agent.host_ops.core.config import HostTarget
from local_agent.host_ops.core.execution import ProcessResult, ProcessState
from local_agent.host_ops.workflows.remote_git import (
    RemoteGitCacheEntry,
    RemoteGitCacheError,
    RemoteGitCacheListResult,
    RemoteGitCacheRemoveResult,
)

cli_main = importlib.import_module("local_agent.host_ops.cli.main")
remote_git_cmd = importlib.import_module("local_agent.host_ops.cli.commands.remote_git")
TARGET = HostTarget(
    alias="worker",
    host="127.0.0.1",
    user="builder",
    identity_file="~/.ssh/key",
)


def _process(*, exit_code: int = 0, stderr: str = "") -> ProcessResult:
    return ProcessResult(
        state=ProcessState.COMPLETED,
        exit_code=exit_code,
        stdout="",
        stderr=stderr,
        duration_seconds=0.1,
    )


def test_remote_git_cache_list_parser() -> None:
    args = cli_main.build_parser().parse_args(
        ["remote", "git", "cache", "list", "worker", "--timeout", "7", "--json"]
    )

    assert args.remote_git_command == "cache"
    assert args.remote_git_cache_command == "list"
    assert args.target == "worker"
    assert args.timeout_seconds == 7.0
    assert args.as_json is True


def test_remote_git_cache_remove_parser() -> None:
    args = cli_main.build_parser().parse_args(
        ["remote", "git", "cache", "remove", "worker", "project-cache"]
    )

    assert args.remote_git_cache_command == "remove"
    assert args.target == "worker"
    assert args.workspace == "project-cache"
    assert args.timeout_seconds == 30.0


def test_run_cache_list_renders_structured_json(
    monkeypatch: pytest.MonkeyPatch,
    capsys,
) -> None:
    class FakeManager:
        def list(self, target, *, limits):
            assert target == TARGET
            assert limits.timeout_seconds == 9.0
            return RemoteGitCacheListResult(
                entries=(
                    RemoteGitCacheEntry(
                        workspace="project",
                        state="ready",
                        repository_url="https://example.invalid/project.git",
                        revision="a" * 40,
                    ),
                ),
                process=_process(),
            )

    monkeypatch.setattr(remote_git_cmd, "load_host_target", lambda alias: TARGET)
    monkeypatch.setattr(remote_git_cmd, "RemoteGitCacheManager", FakeManager)

    assert remote_git_cmd.run_cache_list("worker", timeout_seconds=9.0, as_json=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["target"] == "worker"
    assert payload["entries"][0]["workspace"] == "project"
    assert payload["entries"][0]["state"] == "ready"


def test_run_cache_remove_reports_absent_workspace(
    monkeypatch: pytest.MonkeyPatch,
    capsys,
) -> None:
    class FakeManager:
        def remove(self, target, workspace, *, limits):
            assert target == TARGET
            assert workspace == "project"
            return RemoteGitCacheRemoveResult(
                workspace=workspace,
                existed=False,
                process=_process(),
            )

    monkeypatch.setattr(remote_git_cmd, "load_host_target", lambda alias: TARGET)
    monkeypatch.setattr(remote_git_cmd, "RemoteGitCacheManager", FakeManager)

    assert (
        remote_git_cmd.run_cache_remove("worker", "project", timeout_seconds=5.0, as_json=False)
        == 0
    )
    assert "already absent" in capsys.readouterr().out


def test_run_cache_list_reports_protocol_error(
    monkeypatch: pytest.MonkeyPatch,
    capsys,
) -> None:
    class FakeManager:
        def list(self, target, *, limits):
            raise RemoteGitCacheError("malformed cache evidence")

    monkeypatch.setattr(remote_git_cmd, "load_host_target", lambda alias: TARGET)
    monkeypatch.setattr(remote_git_cmd, "RemoteGitCacheManager", FakeManager)

    assert remote_git_cmd.run_cache_list("worker", timeout_seconds=5.0, as_json=True) == 1
    assert "malformed cache evidence" in json.loads(capsys.readouterr().err)["error"]


def test_run_cache_remove_propagates_busy_exit_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeManager:
        def remove(self, target, workspace, *, limits):
            return RemoteGitCacheRemoveResult(
                workspace=workspace,
                existed=False,
                process=_process(exit_code=75, stderr="remote workspace is busy"),
            )

    monkeypatch.setattr(remote_git_cmd, "load_host_target", lambda alias: TARGET)
    monkeypatch.setattr(remote_git_cmd, "RemoteGitCacheManager", FakeManager)

    assert (
        remote_git_cmd.run_cache_remove("worker", "project", timeout_seconds=5.0, as_json=True)
        == 75
    )
