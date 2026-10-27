from __future__ import annotations

import pytest

from local_agent.host_ops.core.config import HostTarget
from local_agent.host_ops.core.execution import ProcessResult, ProcessState
from local_agent.host_ops.workflows.remote_git import (
    RemoteGitCacheError,
    RemoteGitCacheManager,
    build_cache_list_argv,
    build_cache_remove_argv,
    validate_workspace_name,
)

REVISION = "a" * 40
TARGET = HostTarget(
    alias="worker",
    host="127.0.0.1",
    user="builder",
    identity_file="~/.ssh/key",
)


def _process(
    *,
    exit_code: int = 0,
    stdout: str = "",
    stderr: str = "",
    stdout_truncated: bool = False,
) -> ProcessResult:
    return ProcessResult(
        state=ProcessState.COMPLETED,
        exit_code=exit_code,
        stdout=stdout,
        stderr=stderr,
        duration_seconds=0.1,
        stdout_truncated=stdout_truncated,
    )


class FakeClient:
    def __init__(self, result: ProcessResult) -> None:
        self.result = result
        self.calls: list[tuple[HostTarget, tuple[str, ...], object]] = []

    def execute(self, target, remote_argv, *, limits=None):
        self.calls.append((target, tuple(remote_argv), limits))
        return self.result


def test_workspace_name_validation_is_shared_with_cache() -> None:
    assert validate_workspace_name("project-cache_1") == "project-cache_1"
    with pytest.raises(ValueError, match="workspace"):
        validate_workspace_name("../escape")


def test_cache_list_script_uses_workspace_lock_and_reports_busy() -> None:
    script = build_cache_list_argv()[2]

    assert "workspace-$workspace.lock" in script
    assert "flock -n 8" in script
    assert "printf '%s\\tbusy\\t\\t\\n'" in script
    assert "rm -rf" not in script


def test_cache_list_rejects_broken_marker_repo_and_git_symlinks() -> None:
    script = build_cache_list_argv()[2]

    assert '[[ -e "$repository_marker" || -L "$repository_marker" ]]' in script
    assert '[[ -e "$repo_dir" || -L "$repo_dir" ]]' in script
    assert '-L "$repo_dir/.git"' in script


def test_cache_scripts_do_not_treat_broken_symlinks_as_absent() -> None:
    list_script = build_cache_list_argv()[2]
    remove_script = build_cache_remove_argv("project-cache")[2]

    assert '[[ ! -e "$workspace_root" && ! -L "$workspace_root" ]]' in list_script
    assert '[[ -L "$workspace_root" ||' in remove_script
    assert '[[ ! -e "$workspace_dir" && ! -L "$workspace_dir" ]]' in remove_script


def test_cache_remove_script_uses_same_workspace_lock_without_deleting_it() -> None:
    script = build_cache_remove_argv("project-cache")[2]

    assert "workspace-$workspace.lock" in script
    assert "flock -n 8" in script
    assert 'rm -rf -- "$workspace_dir"' in script
    assert 'rm -f "$lock_dir' not in script


def test_cache_scripts_reject_unsafe_lock_paths_without_truncating() -> None:
    list_script = build_cache_list_argv()[2]
    remove_script = build_cache_remove_argv("project-cache")[2]

    for script in (list_script, remove_script):
        assert '[[ -L "$lock_dir"' in script
        assert '[[ -L "$workspace_lock"' in script
        assert 'exec 8>>"$workspace_lock"' in script
        assert 'exec 8>"' not in script


def test_cache_remove_argv_keeps_workspace_as_separate_argument() -> None:
    argv = build_cache_remove_argv("project-cache")

    assert argv[:2] == ("bash", "-lc")
    assert argv[-1] == "project-cache"
    assert "project-cache" not in argv[2]


def test_cache_remove_argv_rejects_path_like_workspace() -> None:
    with pytest.raises(ValueError, match="workspace"):
        build_cache_remove_argv("../../tmp")


def test_cache_list_parses_ready_busy_and_legacy_entries() -> None:
    client = FakeClient(
        _process(
            stdout=(
                f"project\tready\thttps://example.invalid/project.git\t{REVISION}\n"
                "busy-one\tbusy\t\t\n"
                f"legacy\tlegacy-unbound\t\t{REVISION}\n"
            )
        )
    )

    result = RemoteGitCacheManager(client=client).list(TARGET)

    assert result.ok
    assert [entry.workspace for entry in result.entries] == ["project", "busy-one", "legacy"]
    assert result.entries[0].repository_url == "https://example.invalid/project.git"
    assert result.entries[0].revision == REVISION
    assert result.entries[1].busy
    assert result.entries[2].state == "legacy-unbound"


def test_cache_list_preserves_remote_process_failure_without_parsing() -> None:
    result = RemoteGitCacheManager(client=FakeClient(_process(exit_code=75, stderr="busy"))).list(
        TARGET
    )

    assert not result.ok
    assert result.entries == ()
    assert result.process.exit_code == 75


def test_cache_list_rejects_duplicate_workspace_evidence() -> None:
    stdout = f"project\tready\thttps://example.invalid/a.git\t{REVISION}\n" * 2

    with pytest.raises(RemoteGitCacheError, match="duplicated"):
        RemoteGitCacheManager(client=FakeClient(_process(stdout=stdout))).list(TARGET)


def test_cache_list_rejects_unknown_state() -> None:
    stdout = "project\tmystery\t\t\n"

    with pytest.raises(RemoteGitCacheError, match="unknown state"):
        RemoteGitCacheManager(client=FakeClient(_process(stdout=stdout))).list(TARGET)


def test_cache_list_rejects_truncated_evidence() -> None:
    with pytest.raises(RemoteGitCacheError, match="truncated"):
        RemoteGitCacheManager(
            client=FakeClient(_process(stdout="project\tbusy\t\t\n", stdout_truncated=True))
        ).list(TARGET)


def test_cache_remove_parses_success_marker() -> None:
    client = FakeClient(
        _process(stdout="__HOSTOPS_REMOTE_GIT_CACHE_REMOVED__ workspace=project existed=1\n")
    )

    result = RemoteGitCacheManager(client=client).remove(TARGET, "project")

    assert result.ok
    assert result.workspace == "project"
    assert result.existed


def test_cache_remove_preserves_busy_failure() -> None:
    result = RemoteGitCacheManager(
        client=FakeClient(_process(exit_code=75, stderr="remote workspace is busy"))
    ).remove(TARGET, "project")

    assert not result.ok
    assert result.process.exit_code == 75
    assert not result.existed


def test_cache_remove_rejects_malformed_success_marker() -> None:
    client = FakeClient(
        _process(stdout="__HOSTOPS_REMOTE_GIT_CACHE_REMOVED__ workspace=other existed=1\n")
    )

    with pytest.raises(RemoteGitCacheError, match="malformed"):
        RemoteGitCacheManager(client=client).remove(TARGET, "project")
