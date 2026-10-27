from __future__ import annotations

from local_agent.host_ops.cli.commands import ssh
from local_agent.host_ops.cli.main import build_parser


def test_ssh_exec_parser_preserves_remote_flags_after_separator() -> None:
    args = build_parser().parse_args(["ssh", "exec", "termux-phone", "--json", "--", "uname", "-a"])

    assert args.command == "ssh"
    assert args.ssh_command == "exec"
    assert args.target == "termux-phone"
    assert args.as_json
    assert args.remote_argv == ["uname", "-a"]


def test_ssh_check_parser_accepts_bounded_timeout() -> None:
    args = build_parser().parse_args(["ssh", "check", "termux-phone", "--timeout", "8"])

    assert args.ssh_command == "check"
    assert args.timeout_seconds == 8.0


def test_ssh_push_parser_exposes_transfer_bounds_and_replace_intent() -> None:
    args = build_parser().parse_args(
        [
            "ssh",
            "push",
            "termux-phone",
            "firmware.bin",
            "/data/local/firmware.bin",
            "--replace",
            "--max-bytes",
            "1234",
            "--timeout",
            "12",
            "--json",
        ]
    )

    assert args.ssh_command == "push"
    assert args.target == "termux-phone"
    assert args.local_source == "firmware.bin"
    assert args.remote_destination == "/data/local/firmware.bin"
    assert args.replace is True
    assert args.max_bytes == 1234
    assert args.timeout_seconds == 12.0
    assert args.as_json is True


def test_ssh_pull_parser_uses_bounded_defaults() -> None:
    args = build_parser().parse_args(
        ["ssh", "pull", "termux-phone", "/remote/result.bin", "result.bin"]
    )

    assert args.ssh_command == "pull"
    assert args.remote_source == "/remote/result.bin"
    assert args.local_destination == "result.bin"
    assert args.replace is False
    assert args.max_bytes == ssh.DEFAULT_MAX_TRANSFER_BYTES
    assert args.timeout_seconds == 300.0
    assert args.as_json is False
