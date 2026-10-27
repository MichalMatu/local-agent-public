"""Remote Git workflow runner composed from the SSH capability."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from local_agent.host_ops.capabilities.remote.ssh import SshClient
from local_agent.host_ops.core.config import HostTarget
from local_agent.host_ops.core.execution import ExecutionLimits, ProcessResult

from .command import READY_PREFIX, build_prepare_argv, build_run_argv
from .models import RemoteGitWorkspace


@dataclass(frozen=True, slots=True)
class RemoteGitResult:
    workspace: RemoteGitWorkspace
    process: ProcessResult

    @property
    def prepared(self) -> bool:
        marker = (
            f"{READY_PREFIX} workspace={self.workspace.workspace} "
            f"revision={self.workspace.revision}"
        )
        return any(line == marker for line in self.process.stdout.splitlines())

    @property
    def ok(self) -> bool:
        return self.process.ok and self.prepared

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "prepared": self.prepared,
            "repository_url": self.workspace.repository_url,
            "revision": self.workspace.revision,
            "workspace": self.workspace.workspace,
            "lock": self.workspace.lock,
            "clean_mode": self.workspace.clean_mode,
            "process": self.process.as_dict(),
        }


class RemoteGitRunner:
    def __init__(self, client: SshClient | None = None) -> None:
        self._client = client or SshClient()

    def prepare(
        self,
        target: HostTarget,
        workspace: RemoteGitWorkspace,
        *,
        limits: ExecutionLimits | None = None,
    ) -> RemoteGitResult:
        process = self._client.execute(target, build_prepare_argv(workspace), limits=limits)
        return RemoteGitResult(workspace=workspace, process=process)

    def run(
        self,
        target: HostTarget,
        workspace: RemoteGitWorkspace,
        remote_argv: Sequence[str],
        *,
        limits: ExecutionLimits | None = None,
    ) -> RemoteGitResult:
        process = self._client.execute(
            target,
            build_run_argv(workspace, remote_argv),
            limits=limits,
        )
        return RemoteGitResult(workspace=workspace, process=process)
