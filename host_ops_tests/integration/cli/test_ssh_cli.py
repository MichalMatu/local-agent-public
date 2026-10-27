from __future__ import annotations

from pathlib import Path

import local_agent.host_ops.capabilities.remote.ssh.command as ssh_command
from local_agent.host_ops.cli.main import main


def _write_config(path: Path) -> None:
    path.write_text(
        """version = 1

[hosts.termux-phone]
host = \"192.0.2.100\"
port = 8022
user = \"u0_a123\"
identity_file = \"/tmp/host_ops_test_key\"
""",
        encoding="utf-8",
    )


def _write_fake_ssh(directory: Path) -> Path:
    executable = directory / "ssh"
    executable.write_text(
        """#!/bin/sh
last_arg=''
for arg in \"$@\"; do
    last_arg=$arg
done

if [ \"$last_arg\" = \"id -un\" ]; then
    printf '%s\\n' \"${FAKE_SSH_USER:-u0_a123}\"
    exit \"${FAKE_SSH_EXIT:-0}\"
fi

printf '%s\\n' \"$last_arg\"
exit \"${FAKE_SSH_EXIT:-0}\"
""",
        encoding="utf-8",
    )
    executable.chmod(0o755)
    return executable


def _prepare_environment(tmp_path: Path, monkeypatch) -> None:
    config = tmp_path / "config.toml"
    _write_config(config)
    fake_ssh = _write_fake_ssh(tmp_path)

    monkeypatch.setenv("HOST_OPS_CONFIG", str(config))
    monkeypatch.setattr(ssh_command, "SYSTEM_SSH_EXECUTABLE", str(fake_ssh))


def test_ssh_exec_runs_through_cli_config_and_process_runner(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    _prepare_environment(tmp_path, monkeypatch)

    exit_code = main(
        [
            "ssh",
            "exec",
            "termux-phone",
            "--",
            "printf",
            "%s\\n",
            "hello; definitely-not-a-second-command",
        ]
    )

    captured = capsys.readouterr()
    assert exit_code == 0
    assert captured.err == ""
    assert captured.out == "printf '%s\\n' 'hello; definitely-not-a-second-command'\n"


def test_ssh_exec_propagates_remote_ssh_exit_code(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    _prepare_environment(tmp_path, monkeypatch)
    monkeypatch.setenv("FAKE_SSH_EXIT", "17")

    exit_code = main(["ssh", "exec", "termux-phone", "--", "false"])

    captured = capsys.readouterr()
    assert exit_code == 17
    assert captured.out == "false\n"


def test_ssh_check_verifies_configured_remote_user(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    _prepare_environment(tmp_path, monkeypatch)

    exit_code = main(["ssh", "check", "termux-phone"])

    captured = capsys.readouterr()
    assert exit_code == 0
    assert "SSH check: PASS" in captured.out
    assert "u0_a123" in captured.out
    assert captured.err == ""


def test_ssh_check_fails_on_remote_user_mismatch(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    _prepare_environment(tmp_path, monkeypatch)
    monkeypatch.setenv("FAKE_SSH_USER", "unexpected")

    exit_code = main(["ssh", "check", "termux-phone"])

    captured = capsys.readouterr()
    assert exit_code == 1
    assert "SSH check: FAIL" in captured.err
    assert "remote user mismatch" in captured.err
