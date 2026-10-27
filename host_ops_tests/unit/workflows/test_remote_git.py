from __future__ import annotations

import pytest

from local_agent.host_ops.core.config import HostTarget
from local_agent.host_ops.core.execution import ProcessResult, ProcessState
from local_agent.host_ops.workflows.remote_git import (
    READY_PREFIX,
    RemoteGitRunner,
    RemoteGitWorkspace,
    build_prepare_argv,
    build_run_argv,
    derive_workspace_name,
)

REVISION = "a" * 40


def _workspace(**overrides) -> RemoteGitWorkspace:
    values = {
        "repository_url": "https://github.com/example/project.git",
        "revision": REVISION,
        "workspace": "project",
        "lock": "s22-build",
        "clean_mode": "worktree",
    }
    values.update(overrides)
    return RemoteGitWorkspace(**values)


def _process(*, exit_code: int = 0, stdout: str = "") -> ProcessResult:
    return ProcessResult(
        state=ProcessState.COMPLETED,
        exit_code=exit_code,
        stdout=stdout,
        stderr="",
        duration_seconds=0.1,
    )


def test_workspace_requires_exact_revision() -> None:
    with pytest.raises(ValueError, match="revision"):
        _workspace(revision="main")


def test_workspace_rejects_http_credentials() -> None:
    with pytest.raises(ValueError, match="credentials"):
        _workspace(repository_url="https://user:secret@example.invalid/repo.git")


def test_workspace_allows_ssh_url_with_explicit_user() -> None:
    workspace = _workspace(repository_url="ssh://git@example.invalid/team/project.git")

    assert workspace.repository_url.startswith("ssh://git@")


def test_workspace_rejects_http_query_or_fragment() -> None:
    with pytest.raises(ValueError, match="query or fragment"):
        _workspace(repository_url="https://example.invalid/project.git?auth=value")
    with pytest.raises(ValueError, match="query or fragment"):
        _workspace(repository_url="https://example.invalid/project.git#signed")


def test_workspace_rejects_surrounding_url_whitespace() -> None:
    with pytest.raises(ValueError, match="surrounding whitespace"):
        _workspace(repository_url=" https://example.invalid/project.git")


def test_workspace_identity_is_stable_and_repository_specific() -> None:
    first = derive_workspace_name("https://git.example.test/team/project.git")
    again = derive_workspace_name("https://git.example.test/team/project.git")
    mirror = derive_workspace_name("https://mirror.example.test/team/project.git")

    assert first == again
    assert first != mirror
    assert first.startswith("project-")
    assert len(first) <= 64


def test_prepare_argv_keeps_inputs_as_separate_remote_arguments() -> None:
    workspace = _workspace(repository_url="https://example.invalid/repo with spaces.git")

    argv = build_prepare_argv(workspace)

    assert argv[:2] == ("bash", "-lc")
    assert workspace.repository_url in argv
    assert workspace.revision in argv
    assert workspace.workspace in argv
    assert workspace.repository_url not in argv[2]


def test_prepare_script_removes_partial_clone_on_failure() -> None:
    script = build_prepare_argv(_workspace())[2]

    assert 'rm -rf "$repo_dir"' in script
    assert "partial workspace removed" in script


def test_prepare_script_binds_workspace_to_repository_url() -> None:
    script = build_prepare_argv(_workspace())[2]

    assert "repository-url" in script
    assert "bound to a different repository URL" in script
    assert "legacy remote workspace origin does not match" in script


def test_prepare_script_rejects_symlinked_state_and_nontruncating_locks() -> None:
    script = build_prepare_argv(_workspace())[2]

    assert '[[ -L "$workspace_dir"' in script
    assert '[[ -e "$repository_marker" || -L "$repository_marker" ]]' in script
    assert '[[ -L "$repo_dir"' in script
    assert '-L "$repo_dir/.git"' in script
    assert 'exec 8>>"$workspace_lock"' in script
    assert 'exec 9>>"$host_lock"' in script
    assert 'exec 8>"' not in script
    assert 'exec 9>"' not in script


def test_prepare_script_normalizes_before_and_after_checkout() -> None:
    script = build_prepare_argv(_workspace())[2]

    first_reset = script.index('git -C "$repo_dir" reset --hard\nclean_workspace')
    checkout = script.index('git -C "$repo_dir" checkout --detach "$revision"')
    second_clean = script.index('git -C "$repo_dir" reset --hard "$revision"\nclean_workspace')
    assert first_reset < checkout < second_clean
    assert "clean -ffd" in script
    assert "clean -ffdx" in script
    assert "fetch --prune --prune-tags --tags origin" in script
    assert "for-each-ref" in script
    assert "remote revision was not fetched from origin" in script
    assert "not reachable from fetched origin refs" in script


def test_run_argv_appends_command_without_shell_concatenation() -> None:
    workspace = _workspace()

    argv = build_run_argv(workspace, ("make", "check", "VALUE=a b"))

    assert argv[-3:] == ("make", "check", "VALUE=a b")


def test_result_reports_readiness_even_when_project_command_fails() -> None:
    workspace = _workspace()
    stdout = f"{READY_PREFIX} workspace=project revision={REVISION}\n"

    class FakeClient:
        def execute(self, target, remote_argv, *, limits=None):
            return _process(exit_code=7, stdout=stdout)

    result = RemoteGitRunner(client=FakeClient()).run(
        HostTarget(alias="phone", host="127.0.0.1", user="u", identity_file="~/.ssh/key"),
        workspace,
        ("false",),
    )

    assert result.prepared
    assert not result.ok
    assert result.process.exit_code == 7
