from __future__ import annotations

import importlib
import json

import pytest

from local_agent.host_ops.capabilities.local.serial import SerialTransactionResult

cli_main = importlib.import_module("local_agent.host_ops.cli.main")
serial_cmd = importlib.import_module("local_agent.host_ops.cli.commands.serial")


def test_serial_parser_exposes_bounded_transaction_contract() -> None:
    args = cli_main.build_parser().parse_args(
        [
            "serial",
            "transact",
            "/dev/cu.usbserial-test",
            "--baud",
            "115200",
            "--write-hex",
            "4d3131350a",
            "--read-limit",
            "256",
            "--timeout",
            "2",
            "--idle",
            "0.2",
            "--settle",
            "0.5",
            "--json",
        ]
    )

    assert args.command == "serial"
    assert args.serial_command == "transact"
    assert args.port == "/dev/cu.usbserial-test"
    assert args.baudrate == 115200
    assert args.write_hex == "4d3131350a"
    assert args.read_limit == 256
    assert args.timeout_seconds == 2.0
    assert args.idle_seconds == 0.2
    assert args.settle_seconds == 0.5
    assert args.as_json is True


def test_serial_parser_rejects_two_write_encodings() -> None:
    with pytest.raises(SystemExit):
        cli_main.build_parser().parse_args(
            [
                "serial",
                "transact",
                "/dev/null",
                "--baud",
                "9600",
                "--write-text",
                "PING",
                "--write-hex",
                "50494e47",
            ]
        )


def test_run_transact_renders_json(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    class FakeTransport:
        def transact(self, port: str, **kwargs: object) -> SerialTransactionResult:
            assert port == "/dev/cu.test"
            assert kwargs["baudrate"] == 9600
            assert kwargs["write_data"] == b"PING\n"
            assert kwargs["settle_seconds"] == 0.25
            return SerialTransactionResult(
                requested_port=port,
                resolved_port=port,
                baudrate=9600,
                bytes_written=5,
                data=b"PONG\n",
                elapsed_seconds=0.01,
                deadline_reached=False,
                idle_complete=True,
                read_limit_reached=False,
            )

    monkeypatch.setattr(serial_cmd, "PosixSerialTransport", FakeTransport)

    code = serial_cmd.run_transact(
        "/dev/cu.test",
        baudrate=9600,
        write_text="PING\n",
        write_hex=None,
        read_limit=64,
        timeout_seconds=1.0,
        idle_seconds=0.1,
        settle_seconds=0.25,
        as_json=True,
    )

    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["read_hex"] == "504f4e470a"
    assert payload["read_utf8"] == "PONG\n"


def test_run_transact_rejects_invalid_hex_before_opening_port(capsys) -> None:
    code = serial_cmd.run_transact(
        "/dev/cu.test",
        baudrate=9600,
        write_text=None,
        write_hex="not-hex",
        read_limit=64,
        timeout_seconds=1.0,
        idle_seconds=0.1,
        settle_seconds=0.0,
        as_json=True,
    )

    assert code == 1
    assert "valid hexadecimal" in json.loads(capsys.readouterr().err)["error"]
