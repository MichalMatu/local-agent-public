from __future__ import annotations

from pathlib import Path

from local_agent.host_ops.capabilities.local.git import LocalGitClient, LocalGitError
from local_agent.host_ops.core.execution import ProcessResult, ProcessState

REVISION = "a" * 40


def _result(*, stdout: str = "", stderr: str = "", exit_code: int = 0) -> ProcessResult:
    return ProcessResult(
        state=ProcessState.COMPLETED,
        exit_code=exit_code,
        stdout=stdout,
        stderr=stderr,
        duration_seconds=0.01,
    )


class FakeRunner:
    def __init__(self, results: list[ProcessResult]) -> None:
        self._results = list(results)
        self.calls: list[tuple[tuple[str, ...], Path]] = []

    def run(self, argv, *, cwd=None, env_overrides=None, limits=None):
        assert cwd is not None
        self.calls.append((tuple(argv), cwd))
        return self._results.pop(0)


def test_inspect_resolves_clean_repository_context(tmp_path: Path) -> None:
    root = tmp_path / "example-project"
    root.mkdir()
    runner = FakeRunner(
        [
            _result(stdout=f"{root}\n"),
            _result(),
            _result(stdout=f"{REVISION}\n"),
            _result(stdout="https://git.example.test/team/example-project.git\n"),
        ]
    )

    context = LocalGitClient(runner=runner).inspect(root)

    assert context.root == root.resolve()
    assert context.remote_name == "origin"
    assert context.revision == REVISION
    assert context.remote_url == "https://git.example.test/team/example-project.git"
    assert context.as_dict() == {
        "root": str(root.resolve()),
        "remote_name": "origin",
        "revision": REVISION,
    }
    assert runner.calls[1][0][1:] == (
        "status",
        "--porcelain=v1",
        "--untracked-files=normal",
    )


def test_inspect_rejects_dirty_repository(tmp_path: Path) -> None:
    root = tmp_path / "project"
    root.mkdir()
    runner = FakeRunner(
        [
            _result(stdout=f"{root}\n"),
            _result(stdout=" M src/module.py\n"),
        ]
    )

    try:
        LocalGitClient(runner=runner).inspect(root)
    except LocalGitError as exc:
        assert "must be clean" in str(exc)
    else:
        raise AssertionError("dirty repository should be rejected")
