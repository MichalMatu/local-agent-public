from __future__ import annotations

import shlex
import string
import subprocess
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from local_agent.host_ops.capabilities.remote.ssh import (
    SYSTEM_SCP_EXECUTABLE,
    SYSTEM_SSH_EXECUTABLE,
    SshOptions,
    build_scp_pull_command,
    build_scp_push_command,
    build_ssh_check_command,
    build_ssh_command,
)
from local_agent.host_ops.core.config import HostTarget

_TEST_IDENTITY = "/tmp/host_ops_test_key"
_SHELL_TEXT = st.text(alphabet=string.printable, max_size=40)
_SHELL_PROGRAM = st.text(alphabet=string.printable, min_size=1, max_size=40)


def _target(**overrides) -> HostTarget:
    values = {
        "alias": "phone",
        "host": "example.test",
        "port": 22,
        "user": "user",
        "identity_file": _TEST_IDENTITY,
    }
    values.update(overrides)
    return HostTarget(**values)


def test_build_ssh_command_is_non_interactive_strict_and_non_persistent() -> None:
    target = _target(host="192.0.2.100", port=8022, user="u0_a123")

    command = build_ssh_command(target, ("uname", "-a"))

    assert command[:4] == (SYSTEM_SSH_EXECUTABLE, "-F", "/dev/null", "-T")
    assert "BatchMode=yes" in command
    assert "StrictHostKeyChecking=yes" in command
    assert "PreferredAuthentications=publickey" in command
    assert "PubkeyAuthentication=yes" in command
    assert "PasswordAuthentication=no" in command
    assert "IdentitiesOnly=yes" in command
    assert "AddKeysToAgent=no" in command
    assert "UpdateHostKeys=no" in command
    assert "ClearAllForwardings=yes" in command
    assert "ForwardAgent=no" in command
    assert "PermitLocalCommand=no" in command
    assert "ControlMaster=no" in command
    assert "ControlPersist=no" in command
    assert "ForkAfterAuthentication=no" in command
    assert "LogLevel=ERROR" in command
    assert command[-2] == "u0_a123@192.0.2.100"
    assert command[-1] == "uname -a"


def test_build_scp_push_command_reuses_strict_transport_contract() -> None:
    target = _target(host="192.0.2.100", port=8022, user="u0_a123")

    command = build_scp_push_command(target, Path("/tmp/input.bin"), "/data/local/input.bin")

    assert command[:4] == (SYSTEM_SCP_EXECUTABLE, "-F", "/dev/null", "-B")
    assert "-q" not in command
    assert "BatchMode=yes" in command
    assert "StrictHostKeyChecking=yes" in command
    assert "PasswordAuthentication=no" in command
    assert command[command.index("-P") + 1] == "8022"
    assert command[-2] == "/tmp/input.bin"
    assert command[-1] == "u0_a123@192.0.2.100:/data/local/input.bin"


def test_build_scp_pull_command_brackets_ipv6_destination() -> None:
    command = build_scp_pull_command(
        _target(host="2001:db8::10", user="worker"),
        "/tmp/result.bin",
        Path("/tmp/result.bin"),
    )

    assert command[-2] == "worker@[2001:db8::10]:/tmp/result.bin"
    assert command[-1] == "/tmp/result.bin"


@pytest.mark.parametrize(
    "remote_path",
    [
        "relative.bin",
        "/",
        "/tmp/../secret",
        "/tmp/file with space.bin",
        "/tmp/$variable.bin",
        "/tmp/double//slash.bin",
    ],
)
def test_scp_builders_reject_ambiguous_remote_paths(remote_path: str) -> None:
    with pytest.raises(ValueError, match="remote path"):
        build_scp_push_command(_target(), Path("/tmp/input.bin"), remote_path)


def test_build_ssh_command_uses_only_explicit_identity_file() -> None:
    command = build_ssh_command(_target(), ("true",))

    identity_index = command.index("-i")
    assert command[identity_index + 1] == _TEST_IDENTITY


def test_ssh_and_scp_use_the_same_explicit_known_hosts_file(tmp_path: Path) -> None:
    trust = tmp_path / "known hosts"
    trust.write_text("", encoding="utf-8")
    target = _target(known_hosts_file=str(trust))
    commands = (
        build_ssh_command(target, ("true",)),
        build_scp_push_command(target, Path("/tmp/source"), "/tmp/destination"),
        build_scp_pull_command(target, "/tmp/source", Path("/tmp/destination")),
    )
    for command in commands:
        assert f'UserKnownHostsFile="{trust}"' in command
        assert "StrictHostKeyChecking=yes" in command
        assert "UpdateHostKeys=no" in command
    # Parse with the real client, without opening a network connection.
    parsed = subprocess.run(
        (commands[0][0], "-G", *commands[0][1:]),
        capture_output=True, text=True, timeout=10, check=True,
    )
    assert f"userknownhostsfile {trust}" in parsed.stdout


@pytest.mark.parametrize("kind", ["missing", "directory", "symlink"])
def test_explicit_known_hosts_must_be_a_regular_file(tmp_path: Path, kind: str) -> None:
    path = tmp_path / "trust"
    if kind == "directory":
        path.mkdir()
    elif kind == "symlink":
        target = tmp_path / "real"
        target.write_text("", encoding="utf-8")
        path.symlink_to(target)
    with pytest.raises(ValueError, match="known_hosts_file"):
        build_ssh_command(_target(known_hosts_file=str(path)), ("true",))


def test_build_ssh_command_requires_explicit_remote_user() -> None:
    with pytest.raises(ValueError, match="explicit remote user"):
        build_ssh_command(_target(user=None), ("true",))


def test_build_ssh_command_requires_explicit_identity_file() -> None:
    with pytest.raises(ValueError, match="explicit identity_file"):
        build_ssh_command(_target(identity_file=None), ("true",))


def test_build_ssh_command_quotes_remote_arguments_as_data() -> None:
    command = build_ssh_command(
        _target(),
        ("printf", "%s\\n", "hello; rm -rf /", "$(touch /tmp/pwned)"),
    )

    assert command[-1] == "printf '%s\\n' 'hello; rm -rf /' '$(touch /tmp/pwned)'"


@settings(max_examples=200, deadline=None, derandomize=True, database=None)
@given(program=_SHELL_PROGRAM, arguments=st.lists(_SHELL_TEXT, max_size=6))
def test_build_ssh_command_property_round_trips_remote_arguments(
    program: str, arguments: list[str]
) -> None:
    argv = (program, *arguments)
    command = build_ssh_command(_target(), argv)
    assert tuple(shlex.split(command[-1])) == argv


def test_build_ssh_command_preserves_empty_non_program_argument() -> None:
    command = build_ssh_command(_target(), ("printf", "%s", ""))

    assert command[-1] == "printf %s ''"


def test_build_ssh_check_command_reads_remote_user_identity() -> None:
    command = build_ssh_check_command(_target())

    assert command[-1] == "id -un"


def test_remote_command_rejects_empty_argv() -> None:
    with pytest.raises(ValueError, match="at least one"):
        build_ssh_command(_target(), ())


def test_remote_command_rejects_empty_program_name() -> None:
    with pytest.raises(ValueError, match=r"argv\[0\]"):
        build_ssh_command(_target(), ("", "argument"))


def test_remote_command_rejects_single_string_argv() -> None:
    with pytest.raises(ValueError, match="single string"):
        build_ssh_command(_target(), "uname -a")


def test_remote_command_rejects_nul_byte() -> None:
    with pytest.raises(ValueError, match="NUL"):
        build_ssh_command(_target(), ("printf", "bad\x00value"))


def test_ssh_options_require_positive_integers() -> None:
    with pytest.raises(ValueError):
        SshOptions(connect_timeout_seconds=0)
