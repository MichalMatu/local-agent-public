"""Deterministic system OpenSSH capability."""

from .checks import SshCheckResult
from .client import SshClient
from .command import (
    SYSTEM_SCP_EXECUTABLE,
    SYSTEM_SSH_EXECUTABLE,
    SshOptions,
    build_scp_pull_command,
    build_scp_push_command,
    build_ssh_check_command,
    build_ssh_command,
)
from .resolver import UnknownHostError, resolve_host
from .transfer import (
    DEFAULT_MAX_TRANSFER_BYTES,
    MAX_TRANSFER_BYTES,
    SshFileTransfer,
    SshTransferError,
    SshTransferResult,
)

__all__ = [
    "DEFAULT_MAX_TRANSFER_BYTES",
    "MAX_TRANSFER_BYTES",
    "SYSTEM_SCP_EXECUTABLE",
    "SYSTEM_SSH_EXECUTABLE",
    "SshCheckResult",
    "SshClient",
    "SshFileTransfer",
    "SshOptions",
    "SshTransferError",
    "SshTransferResult",
    "UnknownHostError",
    "build_scp_pull_command",
    "build_scp_push_command",
    "build_ssh_check_command",
    "build_ssh_command",
    "resolve_host",
]
