from __future__ import annotations

import importlib
import json

import pytest

from local_agent.host_ops.capabilities.local.adb import AdbDevice, AdbIdentity, AdbInspectionError

cli_main = importlib.import_module("local_agent.host_ops.cli.main")
adb_cmd = importlib.import_module("local_agent.host_ops.cli.commands.adb")


def test_adb_devices_parser_exposes_bounded_timeout() -> None:
    args = cli_main.build_parser().parse_args(["adb", "devices", "--timeout", "7", "--json"])

    assert args.command == "adb"
    assert args.adb_command == "devices"
    assert args.timeout_seconds == 7.0
    assert args.as_json is True


def test_adb_identity_parser_requires_explicit_serial() -> None:
    args = cli_main.build_parser().parse_args(["adb", "identity", "R58N123ABC"])

    assert args.adb_command == "identity"
    assert args.serial == "R58N123ABC"
    assert args.timeout_seconds == 10.0
    assert args.as_json is False


def test_run_devices_renders_json(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    class FakeClient:
        def devices(self, *, limits):
            assert limits.timeout_seconds == 4.0
            return (
                AdbDevice(
                    serial="ABC",
                    state="device",
                    model="Pixel_8",
                    transport_id="1",
                ),
            )

    monkeypatch.setattr(adb_cmd, "AdbClient", FakeClient)

    assert adb_cmd.run_devices(timeout_seconds=4.0, as_json=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload[0]["serial"] == "ABC"
    assert payload[0]["state"] == "device"


def test_run_identity_renders_selected_properties(
    monkeypatch: pytest.MonkeyPatch,
    capsys,
) -> None:
    class FakeClient:
        def identity(self, serial: str, *, limits):
            assert serial == "ABC"
            assert limits.timeout_seconds == 6.0
            return AdbIdentity(
                serial=serial,
                state="device",
                manufacturer="samsung",
                model="SM-S906B",
                android_version="16",
            )

    monkeypatch.setattr(adb_cmd, "AdbClient", FakeClient)

    assert adb_cmd.run_identity("ABC", timeout_seconds=6.0, as_json=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["manufacturer"] == "samsung"
    assert payload["model"] == "SM-S906B"


def test_run_identity_reports_inspection_error(
    monkeypatch: pytest.MonkeyPatch,
    capsys,
) -> None:
    class FakeClient:
        def identity(self, serial: str, *, limits):
            raise AdbInspectionError(f"ADB device not found: {serial}")

    monkeypatch.setattr(adb_cmd, "AdbClient", FakeClient)

    assert adb_cmd.run_identity("MISSING", timeout_seconds=5.0, as_json=True) == 1
    payload = json.loads(capsys.readouterr().err)
    assert "not found" in payload["error"]
