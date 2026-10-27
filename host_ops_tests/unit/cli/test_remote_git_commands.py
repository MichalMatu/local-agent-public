from __future__ import annotations

import json
from pathlib import Path

from local_agent.host_ops.capabilities.local.git import GitRepositoryContext, LocalGitError
from local_agent.host_ops.cli.commands import remote_git
from local_agent.host_ops.core.execution import ProcessResult, ProcessState
from local_agent.host_ops.workflows.remote_git import READY_PREFIX, RemoteGitWorkspace
from local_agent.host_ops.workflows.remote_git.runner import RemoteGitResult

REVISION = "a" * 40


def _context() -> GitRepositoryContext:
    return GitRepositoryContext(
        root=Path("/repo"),
        remote_name="origin",
        remote_url="https://example.invalid/project.git",
        revision=REVISION,
    )


def _workspace() -> RemoteGitWorkspace:
    return RemoteGitWorkspace(
        repository_url="https://example.invalid/project.git",
        revision=REVISION,
        workspace="project",
        lock="build",
        clean_mode="worktree",
    )


def _process(*, exit_code: int = 0, prepared: bool = True) -> ProcessResult:
    stdout = (
        f"{READY_PREFIX} workspace=project revision={REVISION}\n" if prepared else "plain output\n"
    )
    return ProcessResult(
        state=ProcessState.COMPLETED,
        exit_code=exit_code,
        stdout=stdout,
        stderr="",
        duration_seconds=0.01,
    )


def _result(*, exit_code: int = 0, prepared: bool = True) -> RemoteGitResult:
    return RemoteGitResult(
        workspace=_workspace(), process=_process(exit_code=exit_code, prepared=prepared)
    )


def test_run_prepare_builds_workspace(monkeypatch) -> None:
    captured = {}

    def fake_run_workspace(alias, workspace, **kwargs):
        captured.update(alias=alias, workspace=workspace, kwargs=kwargs)
        return 17

    monkeypatch.setattr(remote_git, "_run_workspace", fake_run_workspace)
    rc = remote_git.run_prepare(
        "phone",
        repository_url="https://example.invalid/repo.git",
        revision=REVISION,
        workspace_name="cache",
        lock="heavy",
        clean_mode="full",
        timeout_seconds=12.0,
        as_json=True,
    )

    assert rc == 17
    assert captured["alias"] == "phone"
    workspace = captured["workspace"]
    assert workspace.repository_url == "https://example.invalid/repo.git"
    assert workspace.workspace == "cache"
    assert workspace.lock == "heavy"
    assert workspace.clean_mode == "full"
    assert captured["kwargs"]["remote_argv"] is None


def test_run_command_forwards_remote_argv(monkeypatch) -> None:
    captured = {}

    def fake_run_workspace(alias, workspace, **kwargs):
        captured.update(alias=alias, workspace=workspace, kwargs=kwargs)
        return 9

    monkeypatch.setattr(remote_git, "_run_workspace", fake_run_workspace)
    rc = remote_git.run_command(
        "phone",
        ("python", "-m", "pytest"),
        repository_url="https://example.invalid/repo.git",
        revision=REVISION,
        workspace_name="cache",
        lock=None,
        clean_mode="worktree",
        timeout_seconds=30.0,
        as_json=False,
    )

    assert rc == 9
    assert captured["kwargs"]["remote_argv"] == ("python", "-m", "pytest")


def test_current_workspace_derives_defaults(monkeypatch) -> None:
    context = _context()

    class FakeLocalGitClient:
        def inspect(self, path, **kwargs):
            assert path == Path("src")
            assert kwargs["remote_name"] == "upstream"
            assert kwargs["require_clean"] is True
            return context

    monkeypatch.setattr(remote_git, "LocalGitClient", FakeLocalGitClient)
    monkeypatch.setattr(remote_git, "derive_workspace_name", lambda url: "derived-cache")
    actual_context, workspace = remote_git._current_workspace(
        source_path="src",
        remote_name="upstream",
        repository_url_override=None,
        workspace_name=None,
        lock="heavy",
        clean_mode="full",
    )

    assert actual_context is context
    assert workspace.repository_url == context.remote_url
    assert workspace.revision == context.revision
    assert workspace.workspace == "derived-cache"
    assert workspace.lock == "heavy"
    assert workspace.clean_mode == "full"


def test_current_workspace_honors_overrides(monkeypatch) -> None:
    context = _context()

    class FakeLocalGitClient:
        def inspect(self, path, **kwargs):
            return context

    monkeypatch.setattr(remote_git, "LocalGitClient", FakeLocalGitClient)
    _, workspace = remote_git._current_workspace(
        source_path=".",
        remote_name="origin",
        repository_url_override="https://mirror.invalid/project.git",
        workspace_name="chosen",
        lock=None,
        clean_mode="worktree",
    )

    assert workspace.repository_url == "https://mirror.invalid/project.git"
    assert workspace.workspace == "chosen"


def test_prepare_current_reports_local_git_error_as_json(monkeypatch, capsys) -> None:
    def fail_current_workspace(**kwargs):
        raise LocalGitError("dirty worktree")

    monkeypatch.setattr(remote_git, "_current_workspace", fail_current_workspace)
    rc = remote_git.run_prepare_current(
        "phone",
        source_path=".",
        remote_name="origin",
        repository_url_override=None,
        workspace_name=None,
        lock=None,
        clean_mode="worktree",
        timeout_seconds=20.0,
        as_json=True,
    )

    assert rc == 2
    assert json.loads(capsys.readouterr().out) == {"ok": False, "error": "dirty worktree"}


def test_run_current_reports_validation_error(monkeypatch, capsys) -> None:
    def fail_current_workspace(**kwargs):
        raise ValueError("bad repository")

    monkeypatch.setattr(remote_git, "_current_workspace", fail_current_workspace)
    rc = remote_git.run_current(
        "phone",
        ("true",),
        source_path=".",
        remote_name="origin",
        repository_url_override=None,
        workspace_name=None,
        lock=None,
        clean_mode="worktree",
        timeout_seconds=20.0,
        as_json=False,
    )

    assert rc == 2
    assert "bad repository" in capsys.readouterr().err


def test_prepare_current_passes_local_context(monkeypatch) -> None:
    context = _context()
    workspace = _workspace()
    captured = {}
    monkeypatch.setattr(remote_git, "_current_workspace", lambda **kwargs: (context, workspace))

    def fake_run_workspace(alias, actual_workspace, **kwargs):
        captured.update(alias=alias, workspace=actual_workspace, kwargs=kwargs)
        return 0

    monkeypatch.setattr(remote_git, "_run_workspace", fake_run_workspace)
    assert (
        remote_git.run_prepare_current(
            "phone",
            source_path=".",
            remote_name="origin",
            repository_url_override=None,
            workspace_name=None,
            lock=None,
            clean_mode="worktree",
            timeout_seconds=20.0,
            as_json=False,
        )
        == 0
    )
    assert captured["kwargs"]["local_context"] is context
    assert captured["kwargs"]["remote_argv"] is None


def test_run_current_passes_command_and_context(monkeypatch) -> None:
    context = _context()
    workspace = _workspace()
    captured = {}
    monkeypatch.setattr(remote_git, "_current_workspace", lambda **kwargs: (context, workspace))

    def fake_run_workspace(alias, actual_workspace, **kwargs):
        captured.update(alias=alias, workspace=actual_workspace, kwargs=kwargs)
        return 0

    monkeypatch.setattr(remote_git, "_run_workspace", fake_run_workspace)
    assert (
        remote_git.run_current(
            "phone",
            ("make", "check"),
            source_path=".",
            remote_name="origin",
            repository_url_override=None,
            workspace_name=None,
            lock=None,
            clean_mode="worktree",
            timeout_seconds=20.0,
            as_json=True,
        )
        == 0
    )
    assert captured["kwargs"]["local_context"] is context
    assert captured["kwargs"]["remote_argv"] == ("make", "check")


def test_run_workspace_prepare_emits_json_with_local_context(monkeypatch, capsys) -> None:
    result = _result()

    class FakeRunner:
        def prepare(self, target, workspace, *, limits=None):
            return result

    monkeypatch.setattr(remote_git, "load_host_target", lambda alias: object())
    monkeypatch.setattr(remote_git, "RemoteGitRunner", FakeRunner)
    rc = remote_git._run_workspace(
        "phone",
        _workspace(),
        timeout_seconds=20.0,
        as_json=True,
        remote_argv=None,
        local_context=_context(),
    )

    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["target"] == "phone"
    assert payload["prepared"] is True
    assert payload["local_repository"]["revision"] == REVISION


def test_run_workspace_executes_command_and_human_output(monkeypatch) -> None:
    result = _result()
    emitted = []

    class FakeRunner:
        def run(self, target, workspace, remote_argv, *, limits=None):
            assert remote_argv == ("make", "check")
            return result

    monkeypatch.setattr(remote_git, "load_host_target", lambda alias: object())
    monkeypatch.setattr(remote_git, "RemoteGitRunner", FakeRunner)
    monkeypatch.setattr(remote_git, "emit_process_output", lambda process: emitted.append(process))
    rc = remote_git._run_workspace(
        "phone",
        _workspace(),
        timeout_seconds=20.0,
        as_json=False,
        remote_argv=("make", "check"),
    )

    assert rc == 0
    assert emitted == [result.process]


def test_run_workspace_rejects_missing_readiness(monkeypatch, capsys) -> None:
    result = _result(prepared=False)

    class FakeRunner:
        def prepare(self, target, workspace, *, limits=None):
            return result

    monkeypatch.setattr(remote_git, "load_host_target", lambda alias: object())
    monkeypatch.setattr(remote_git, "RemoteGitRunner", FakeRunner)
    monkeypatch.setattr(remote_git, "emit_process_output", lambda process: None)
    rc = remote_git._run_workspace(
        "phone",
        _workspace(),
        timeout_seconds=20.0,
        as_json=False,
        remote_argv=None,
    )

    assert rc == 1
    assert "without readiness evidence" in capsys.readouterr().err


def test_run_workspace_json_ok_matches_missing_readiness_exit(monkeypatch, capsys) -> None:
    result = _result(prepared=False)

    class FakeRunner:
        def prepare(self, target, workspace, *, limits=None):
            return result

    monkeypatch.setattr(remote_git, "load_host_target", lambda alias: object())
    monkeypatch.setattr(remote_git, "RemoteGitRunner", FakeRunner)
    rc = remote_git._run_workspace(
        "phone",
        _workspace(),
        timeout_seconds=20.0,
        as_json=True,
        remote_argv=None,
    )

    payload = json.loads(capsys.readouterr().out)
    assert rc == 1
    assert payload["ok"] is False
    assert payload["prepared"] is False
    assert payload["process"]["exit_code"] == 0


def test_run_workspace_json_success_contract_and_key_set(monkeypatch, capsys) -> None:
    result = _result(prepared=True)

    class FakeRunner:
        def prepare(self, target, workspace, *, limits=None):
            return result

    monkeypatch.setattr(remote_git, "load_host_target", lambda alias: object())
    monkeypatch.setattr(remote_git, "RemoteGitRunner", FakeRunner)
    rc = remote_git._run_workspace(
        "phone",
        _workspace(),
        timeout_seconds=20.0,
        as_json=True,
        remote_argv=None,
    )

    payload = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert payload["ok"] is True
    assert payload["prepared"] is True
    assert set(payload) == {
        "target",
        "ok",
        "prepared",
        "repository_url",
        "revision",
        "workspace",
        "lock",
        "clean_mode",
        "process",
    }


def test_run_workspace_json_process_failure_keeps_readiness_truth(monkeypatch, capsys) -> None:
    result = _result(exit_code=7, prepared=True)

    class FakeRunner:
        def run(self, target, workspace, remote_argv, *, limits=None):
            return result

    monkeypatch.setattr(remote_git, "load_host_target", lambda alias: object())
    monkeypatch.setattr(remote_git, "RemoteGitRunner", FakeRunner)
    rc = remote_git._run_workspace(
        "phone",
        _workspace(),
        timeout_seconds=20.0,
        as_json=True,
        remote_argv=("false",),
    )

    payload = json.loads(capsys.readouterr().out)
    assert rc == 7
    assert payload["ok"] is False
    assert payload["prepared"] is True
    assert payload["process"]["exit_code"] == 7


def test_run_workspace_json_current_adds_only_local_repository(monkeypatch, capsys) -> None:
    result = _result(prepared=True)

    class FakeRunner:
        def prepare(self, target, workspace, *, limits=None):
            return result

    monkeypatch.setattr(remote_git, "load_host_target", lambda alias: object())
    monkeypatch.setattr(remote_git, "RemoteGitRunner", FakeRunner)
    rc = remote_git._run_workspace(
        "phone",
        _workspace(),
        timeout_seconds=20.0,
        as_json=True,
        remote_argv=None,
        local_context=_context(),
    )

    payload = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert set(payload) == {
        "target",
        "ok",
        "prepared",
        "repository_url",
        "revision",
        "workspace",
        "lock",
        "clean_mode",
        "process",
        "local_repository",
    }


def test_run_workspace_reports_target_error(monkeypatch, capsys) -> None:
    def fail_target(alias):
        raise remote_git.HostTargetResolutionError("unknown target")

    monkeypatch.setattr(remote_git, "load_host_target", fail_target)
    rc = remote_git._run_workspace(
        "missing",
        _workspace(),
        timeout_seconds=20.0,
        as_json=True,
        remote_argv=None,
    )

    assert rc == 2
    assert json.loads(capsys.readouterr().out)["error"] == "unknown target"
