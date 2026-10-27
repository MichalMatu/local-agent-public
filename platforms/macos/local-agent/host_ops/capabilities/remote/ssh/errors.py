"""Typed SSH transfer errors and bounded partial-effect evidence."""

from __future__ import annotations

from local_agent.host_ops.core.execution import ProcessResult


class SshTransferError(RuntimeError):
    """Failure with a structured phase, process evidence and truthful effects."""

    def __init__(
        self,
        message: str,
        *,
        action_attempted: bool = False,
        committed: bool = False,
        cleanup_failed: bool = False,
        commit_uncertain: bool = False,
        code: str = "ssh_transfer_failed",
        phase: str | None = None,
        process: ProcessResult | None = None,
    ) -> None:
        super().__init__(message)
        self.action_attempted = action_attempted or committed or commit_uncertain
        self.committed = committed
        self.cleanup_failed = cleanup_failed
        self.commit_uncertain = commit_uncertain and not committed
        self.code = code
        self.phase = phase
        self.process = process


def _input_error(message: str) -> SshTransferError:
    return SshTransferError(message, code="ssh_invalid_input", phase="preflight")


def _conflict_error(message: str) -> SshTransferError:
    return SshTransferError(message, code="ssh_destination_conflict", phase="preflight")


def _integrity_error(message: str) -> SshTransferError:
    return SshTransferError(message, code="ssh_integrity_mismatch", phase="verification")


