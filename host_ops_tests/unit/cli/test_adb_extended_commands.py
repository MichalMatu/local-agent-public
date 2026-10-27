from __future__ import annotations

import importlib
import json

import pytest

from local_agent.host_ops.capabilities.local.adb import (
    AdbLogcatResult,
    AdbTransferError,
    AdbTransferResult,
)

cli_main = importlib.import_module("local_agent.host_ops.cli.main")
adb_cmd = importlib.import_module("local_agent.host_ops.cli.commands.adb")


def test_adb_logcat_parser_exposes_explicit_serial_and_line_bound() -> None:
    args = cli_main.build_parser().parse_args(
        ["adb", "logcat", "ABC", "--lines", "50", "--timeout", "4", "--json"]
    )

    assert args.adb_command == "logcat"
    assert args.serial == "ABC"
    assert args.lines == 50
    assert args.timeout_seconds == 4.0
    assert args.as_json is True


def test_adb_push_and_pull_parsers_expose_transfer_bounds() -> None:
    push = cli_main.build_parser().parse_args(
        [
            "adb",
            "push",
            "ABC",
            "input.bin",
            "/sdcard/input.bin",
            "--replace",
            "--max-bytes",
            "99",
            "--timeout",
            "7",
            "--json",
        ]
    )
    pull = cli_main.build_parser().parse_args(["adb", "pull", "ABC", "/sdcard/out.bin", "out.bin"])

    assert push.adb_command == "push"
    assert push.serial == "ABC"
    assert push.local_source == "input.bin"
    assert push.remote_destination == "/sdcard/input.bin"
    assert push.replace is True
    assert push.max_bytes == 99
    assert push.timeout_seconds == 7.0
    assert pull.adb_command == "pull"
    assert pull.remote_source == "/sdcard/out.bin"
    assert pull.local_destination == "out.bin"
    assert pull.replace is False
    assert pull.max_bytes == 512 * 1024 * 1024


def test_run_logcat_renders_json(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    class FakeClient:
        def logcat(self, serial: str, *, lines: int, limits):
            assert serial == "ABC"
            assert lines == 20
            assert limits.timeout_seconds == 3.0
            return AdbLogcatResult(
                serial=serial,
                lines_requested=lines,
                line_count=2,
                text="one\ntwo\n",
            )

    monkeypatch.setattr(adb_cmd, "AdbClient", FakeClient)

    assert adb_cmd.run_logcat("ABC", lines=20, timeout_seconds=3.0, as_json=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["line_count"] == 2
    assert payload["text"] == "one\ntwo\n"


def test_run_push_renders_verified_transfer(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    class FakeTransfer:
        def push(self, serial, source, destination, *, replace, max_bytes, limits):
            assert serial == "ABC"
            assert str(source) == "input.bin"
            assert destination == "/sdcard/input.bin"
            assert replace is True
            assert max_bytes == 100
            assert limits.timeout_seconds == 8.0
            return AdbTransferResult(
                direction="push",
                serial=serial,
                source=str(source),
                destination=destination,
                size_bytes=7,
                sha256="a" * 64,
                replaced_existing=True,
            )

    monkeypatch.setattr(adb_cmd, "AdbFileTransfer", FakeTransfer)

    assert (
        adb_cmd.run_push(
            "ABC",
            "input.bin",
            "/sdcard/input.bin",
            replace=True,
            max_bytes=100,
            timeout_seconds=8.0,
            as_json=True,
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["direction"] == "push"
    assert payload["replaced_existing"] is True


def test_run_pull_propagates_transfer_error(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    class FakeTransfer:
        def pull(self, *args, **kwargs):
            raise AdbTransferError(
                "remote source is not a regular file",
                action_attempted=True,
                committed=True,
                cleanup_failed=False,
            )

    monkeypatch.setattr(adb_cmd, "AdbFileTransfer", FakeTransfer)

    assert (
        adb_cmd.run_pull(
            "ABC",
            "/sdcard/missing.bin",
            "out.bin",
            replace=False,
            max_bytes=100,
            timeout_seconds=5.0,
            as_json=True,
        )
        == 1
    )
    payload = json.loads(capsys.readouterr().err)
    assert "not a regular file" in payload["error"]
    assert payload["action_attempted"] is True
    assert payload["committed"] is True
    assert payload["cleanup_failed"] is False
