"""Read deterministic context from a local Git repository."""

from __future__ import annotations

import re
from pathlib import Path

from local_agent.host_ops.core.execution import ExecutionLimits, ProcessResult, ProcessRunner

from .models import GitRepositoryContext

_REMOTE_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,127}$")


class LocalGitError(RuntimeError):
    """Raised when local repository context cannot be resolved safely."""


class LocalGitClient:
    def __init__(self, runner: ProcessRunner | None = None, *, git_executable: str = "git") -> None:
        self._runner = runner or ProcessRunner()
        self._git_executable = git_executable

    def inspect(
        self,
        path: Path | None = None,
        *,
        remote_name: str = "origin",
        require_clean: bool = True,
        limits: ExecutionLimits | None = None,
    ) -> GitRepositoryContext:
        if not isinstance(remote_name, str) or not _REMOTE_NAME_PATTERN.fullmatch(remote_name):
            raise ValueError("remote_name contains unsupported characters")

        start = (path or Path.cwd()).expanduser().resolve()
        root_result = self._run(("rev-parse", "--show-toplevel"), cwd=start, limits=limits)
        root = Path(_require_stdout(root_result, "resolve repository root")).resolve()

        if require_clean:
            status = self._run(
                ("status", "--porcelain=v1", "--untracked-files=normal"),
                cwd=root,
                limits=limits,
            )
            _require_ok(status, "inspect repository status")
            if status.stdout.strip():
                raise LocalGitError(
                    "local Git worktree must be clean so the remote host can reproduce exact HEAD"
                )

        revision = _require_stdout(
            self._run(("rev-parse", "--verify", "HEAD^{commit}"), cwd=root, limits=limits),
            "resolve HEAD commit",
        )
        remote_url = _require_stdout(
            self._run(("remote", "get-url", remote_name), cwd=root, limits=limits),
            f"resolve Git remote {remote_name!r}",
        )
        return GitRepositoryContext(
            root=root,
            remote_name=remote_name,
            remote_url=remote_url,
            revision=revision,
        )

    def _run(
        self,
        arguments: tuple[str, ...],
        *,
        cwd: Path,
        limits: ExecutionLimits | None,
    ) -> ProcessResult:
        return self._runner.run((self._git_executable, *arguments), cwd=cwd, limits=limits)


def _require_ok(result: ProcessResult, action: str) -> None:
    if result.ok:
        return
    detail = result.stderr.strip() or result.error or f"exit_code={result.exit_code}"
    raise LocalGitError(f"could not {action}: {detail}")


def _require_stdout(result: ProcessResult, action: str) -> str:
    _require_ok(result, action)
    value = result.stdout.strip()
    if not value or "\n" in value:
        raise LocalGitError(f"could not {action}: unexpected Git output")
    return value
