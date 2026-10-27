"""Build deterministic system OpenSSH invocations."""

from __future__ import annotations

import shlex
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from local_agent.host_ops.core.config import HostTarget

from .paths import normalize_remote_file_path

SYSTEM_SSH_EXECUTABLE = "/usr/bin/ssh"
SYSTEM_SCP_EXECUTABLE = "/usr/bin/scp"


@dataclass(frozen=True, slots=True)
class SshOptions:
    connect_timeout_seconds: int = 10
    server_alive_interval_seconds: int = 15
    server_alive_count_max: int = 2

    def __post_init__(self) -> None:
        for name, value in (
            ("connect_timeout_seconds", self.connect_timeout_seconds),
            ("server_alive_interval_seconds", self.server_alive_interval_seconds),
            ("server_alive_count_max", self.server_alive_count_max),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{name} must be an integer greater than zero")


def build_ssh_command(
    target: HostTarget,
    remote_argv: Sequence[str],
    *,
    options: SshOptions | None = None,
) -> tuple[str, ...]:
    normalized_remote = _normalize_remote_argv(remote_argv)
    applied = options or SshOptions()
    destination = _destination(target)
    remote_command = shlex.join(normalized_remote)

    return (
        SYSTEM_SSH_EXECUTABLE,
        "-F",
        "/dev/null",
        "-T",
        *_transport_options(applied),
        *_known_hosts_options(target),
        "-i",
        _required_identity_file(target),
        "-p",
        str(target.port),
        destination,
        remote_command,
    )


def build_ssh_check_command(
    target: HostTarget,
    *,
    options: SshOptions | None = None,
) -> tuple[str, ...]:
    return build_ssh_command(target, ("id", "-un"), options=options)


def build_scp_push_command(
    target: HostTarget,
    local_source: Path,
    remote_destination: str,
    *,
    options: SshOptions | None = None,
) -> tuple[str, ...]:
    applied = options or SshOptions()
    return (
        SYSTEM_SCP_EXECUTABLE,
        "-F",
        "/dev/null",
        "-B",
        *_transport_options(applied),
        *_known_hosts_options(target),
        "-i",
        _required_identity_file(target),
        "-P",
        str(target.port),
        str(local_source),
        _remote_spec(target, remote_destination),
    )


def build_scp_pull_command(
    target: HostTarget,
    remote_source: str,
    local_destination: Path,
    *,
    options: SshOptions | None = None,
) -> tuple[str, ...]:
    applied = options or SshOptions()
    return (
        SYSTEM_SCP_EXECUTABLE,
        "-F",
        "/dev/null",
        "-B",
        *_transport_options(applied),
        *_known_hosts_options(target),
        "-i",
        _required_identity_file(target),
        "-P",
        str(target.port),
        _remote_spec(target, remote_source),
        str(local_destination),
    )


def _transport_options(options: SshOptions) -> tuple[str, ...]:
    return (
        "-o",
        "BatchMode=yes",
        "-o",
        "StrictHostKeyChecking=yes",
        "-o",
        "PreferredAuthentications=publickey",
        "-o",
        "PubkeyAuthentication=yes",
        "-o",
        "PasswordAuthentication=no",
        "-o",
        "IdentitiesOnly=yes",
        "-o",
        "AddKeysToAgent=no",
        "-o",
        "UpdateHostKeys=no",
        "-o",
        "ClearAllForwardings=yes",
        "-o",
        "ForwardAgent=no",
        "-o",
        "PermitLocalCommand=no",
        "-o",
        "ControlMaster=no",
        "-o",
        "ControlPersist=no",
        "-o",
        "ForkAfterAuthentication=no",
        "-o",
        f"ConnectTimeout={options.connect_timeout_seconds}",
        "-o",
        f"ServerAliveInterval={options.server_alive_interval_seconds}",
        "-o",
        f"ServerAliveCountMax={options.server_alive_count_max}",
        "-o",
        "LogLevel=ERROR",
    )


def _known_hosts_options(target: HostTarget) -> tuple[str, ...]:
    if target.known_hosts_file is None:
        return ()
    path = Path(target.known_hosts_file).expanduser()
    if path.is_symlink() or not path.is_file():
        raise ValueError("known_hosts_file must be an existing regular file without a symlink")
    # OpenSSH parses this option as a list: quote the single literal path.
    return ("-o", f'UserKnownHostsFile="{path}"')


def _destination(target: HostTarget) -> str:
    if target.user is None:
        raise ValueError("SSH target requires an explicit remote user")
    return f"{target.user}@{target.host}"


def _remote_spec(target: HostTarget, remote_path: str) -> str:
    normalized = normalize_remote_file_path(remote_path)
    if target.user is None:
        raise ValueError("SSH target requires an explicit remote user")
    host = f"[{target.host}]" if ":" in target.host else target.host
    return f"{target.user}@{host}:{normalized}"


def _required_identity_file(target: HostTarget) -> str:
    if target.identity_file is None:
        raise ValueError("SSH target requires an explicit identity_file")
    try:
        return str(Path(target.identity_file).expanduser())
    except RuntimeError as exc:
        raise ValueError(f"could not resolve identity_file: {target.identity_file!r}") from exc


def _normalize_remote_argv(remote_argv: Sequence[str]) -> tuple[str, ...]:
    if isinstance(remote_argv, str | bytes):
        raise ValueError("remote command must be an argument sequence, not a single string")
    normalized = tuple(remote_argv)
    if not normalized:
        raise ValueError("remote command must contain at least one argument")
    if not isinstance(normalized[0], str) or not normalized[0]:
        raise ValueError("remote command argv[0] must be a non-empty string")
    if any(not isinstance(value, str) for value in normalized[1:]):
        raise ValueError("remote command arguments after argv[0] must be strings")
    if any("\x00" in value for value in normalized):
        raise ValueError("remote command arguments must not contain NUL bytes")
    return normalized
