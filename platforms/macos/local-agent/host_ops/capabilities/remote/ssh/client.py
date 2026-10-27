"""System OpenSSH adapter backed by the shared ProcessRunner."""

from __future__ import annotations

from collections.abc import Sequence

from local_agent.host_ops.core.config import HostTarget
from local_agent.host_ops.core.execution import ExecutionLimits, ProcessResult, ProcessRunner

from .checks import SshCheckResult
from .command import SshOptions, build_ssh_check_command, build_ssh_command


class SshClient:
    def __init__(
        self,
        runner: ProcessRunner | None = None,
        *,
        options: SshOptions | None = None,
    ) -> None:
        self._runner = runner or ProcessRunner()
        self._options = options or SshOptions()

    def execute(
        self,
        target: HostTarget,
        remote_argv: Sequence[str],
        *,
        limits: ExecutionLimits | None = None,
    ) -> ProcessResult:
        command = build_ssh_command(target, remote_argv, options=self._options)
        return self._runner.run(command, limits=limits)

    def execute_binary_to_fd(
        self,
        target: HostTarget,
        remote_argv: Sequence[str],
        *,
        sink_fd: int,
        max_bytes: int,
        limits: ExecutionLimits | None = None,
    ) -> ProcessResult:
        """Stream remote stdout as raw bytes through the shared supervised runner."""
        command = build_ssh_command(target, remote_argv, options=self._options)
        return self._runner.run(
            command,
            limits=limits,
            binary_stdout_fd=sink_fd,
            binary_stdout_limit=max_bytes,
        )

    def check(
        self,
        target: HostTarget,
        *,
        limits: ExecutionLimits | None = None,
    ) -> SshCheckResult:
        command = build_ssh_check_command(target, options=self._options)
        process = self._runner.run(command, limits=limits)
        remote_user = process.stdout.strip() if process.ok else None
        return SshCheckResult(
            process=process,
            expected_user=target.user,
            remote_user=remote_user,
        )
