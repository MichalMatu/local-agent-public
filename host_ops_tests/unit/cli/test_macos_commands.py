from __future__ import annotations

import importlib
import json

from local_agent.host_ops.capabilities.local.macos import (
    MacOSHostInfo,
    MacOSInspectionError,
    MacOSStorageDevice,
    MacOSUSBDevice,
)
from local_agent.host_ops.cli.commands import macos

cli_main = importlib.import_module("local_agent.host_ops.cli.main")


class FakeInspector:
    def host_info(self):
        return MacOSHostInfo(product_version="15.6.1", architecture="arm64")

    def usb_devices(self):
        return (MacOSUSBDevice(name="Adapter", vendor_id="0x1234", product_id="0xabcd"),)

    def serial_ports(self):
        return ()

    def external_storage(self):
        return (
            MacOSStorageDevice(
                identifier="disk4s1",
                device_node="/dev/disk4s1",
                whole=False,
                volume_name="FIRMWARE",
                mount_point="/Volumes/FIRMWARE",
                filesystem="MS-DOS FAT32",
                internal=False,
            ),
        )


class FailingInspector:
    def host_info(self):
        raise MacOSInspectionError("unavailable")

    def usb_devices(self):
        raise MacOSInspectionError("unavailable")

    def serial_ports(self):
        raise MacOSInspectionError("unavailable")

    def external_storage(self):
        raise MacOSInspectionError("unavailable")


def test_macos_parser_exposes_all_read_only_commands() -> None:
    parser = cli_main.build_parser()

    for command in ("host", "usb", "serial", "storage"):
        args = parser.parse_args(["macos", command, "--json"])
        assert args.command == "macos"
        assert args.macos_command == command
        assert args.as_json is True


def test_macos_host_json_dispatch(monkeypatch, capsys) -> None:
    monkeypatch.setattr(macos, "MacOSInspector", FakeInspector)

    result = cli_main.main(["macos", "host", "--json"])

    assert result == 0
    assert json.loads(capsys.readouterr().out) == {
        "architecture": "arm64",
        "product_version": "15.6.1",
    }


def test_macos_host_human_output(monkeypatch, capsys) -> None:
    monkeypatch.setattr(macos, "MacOSInspector", FakeInspector)

    assert cli_main.main(["macos", "host"]) == 0
    assert capsys.readouterr().out.strip() == "macOS 15.6.1 (arm64)"


def test_macos_usb_human_output(monkeypatch, capsys) -> None:
    monkeypatch.setattr(macos, "MacOSInspector", FakeInspector)

    result = cli_main.main(["macos", "usb"])

    assert result == 0
    output = capsys.readouterr().out
    assert "name=Adapter" in output
    assert "vendor_id=0x1234" in output


def test_macos_empty_serial_output(monkeypatch, capsys) -> None:
    monkeypatch.setattr(macos, "MacOSInspector", FakeInspector)

    result = cli_main.main(["macos", "serial"])

    assert result == 0
    assert capsys.readouterr().out.strip() == "No serial ports found"


def test_macos_storage_json_output(monkeypatch, capsys) -> None:
    monkeypatch.setattr(macos, "MacOSInspector", FakeInspector)

    assert cli_main.main(["macos", "storage", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload[0]["identifier"] == "disk4s1"
    assert payload[0]["mount_point"] == "/Volumes/FIRMWARE"


def test_macos_error_rendering_in_json_and_human_modes(monkeypatch, capsys) -> None:
    monkeypatch.setattr(macos, "MacOSInspector", FailingInspector)

    assert cli_main.main(["macos", "usb", "--json"]) == 1
    json_error = json.loads(capsys.readouterr().err)
    assert json_error == {"error": "unavailable"}

    assert cli_main.main(["macos", "storage"]) == 1
    assert "macOS inspection failed: unavailable" in capsys.readouterr().err
