from __future__ import annotations

from local_agent.host_ops.capabilities.remote.ssh import SYSTEM_SSH_EXECUTABLE, SshClient
from local_agent.host_ops.core.config import HostTarget
from local_agent.host_ops.core.execution import ExecutionLimits, ProcessResult, ProcessState

_TEST_IDENTITY = "/tmp/host_ops_test_key"


class FakeRunner:
    def __init__(self, result: ProcessResult) -> None:
        self.result = result
        self.command: tuple[str, ...] | None = None
        self.limits: ExecutionLimits | None = None

    def run(self, command, *, limits=None):
        self.command = tuple(command)
        self.limits = limits
        return self.result


def _completed(stdout: str = "", *, exit_code: int = 0) -> ProcessResult:
    return ProcessResult(
        state=ProcessState.COMPLETED,
        exit_code=exit_code,
        stdout=stdout,
        stderr="",
        duration_seconds=0.01,
    )


def _target(*, user: str = "user") -> HostTarget:
    return HostTarget(
        alias="phone",
        host="example.test",
        user=user,
        identity_file=_TEST_IDENTITY,
    )


def test_execute_delegates_only_to_shared_process_runner() -> None:
    runner = FakeRunner(_completed())
    limits = ExecutionLimits(timeout_seconds=7)

    result = SshClient(runner).execute(_target(), ("uname", "-a"), limits=limits)

    assert result.ok
    assert runner.command is not None
    assert runner.command[0] == SYSTEM_SSH_EXECUTABLE
    assert runner.command[-1] == "uname -a"
    assert runner.limits is limits


def test_check_verifies_expected_remote_user() -> None:
    runner = FakeRunner(_completed("u0_a123\n"))

    result = SshClient(runner).check(_target(user="u0_a123"))

    assert result.ok
    assert result.identity_matches
    assert result.remote_user == "u0_a123"
    assert runner.command is not None
    assert runner.command[-1] == "id -un"


def test_check_rejects_remote_user_mismatch() -> None:
    runner = FakeRunner(_completed("wrong-user\n"))

    result = SshClient(runner).check(_target(user="u0_a123"))

    assert not result.ok
    assert not result.identity_matches
    assert result.process.ok
