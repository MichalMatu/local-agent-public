from __future__ import annotations

import importlib
import plistlib

import pytest

from local_agent.host_ops.capabilities.local.macos import MacOSInspectionError, MacOSInspector
from local_agent.host_ops.core.execution import ExecutionLimits, ProcessResult, ProcessState

inspection_module = importlib.import_module(
    "local_agent.host_ops.capabilities.local.macos.inspection"
)


def _result(
    *,
    stdout: str = "",
    stderr: str = "",
    exit_code: int = 0,
    stdout_truncated: bool = False,
) -> ProcessResult:
    return ProcessResult(
        state=ProcessState.COMPLETED,
        exit_code=exit_code,
        stdout=stdout,
        stderr=stderr,
        duration_seconds=0.01,
        stdout_truncated=stdout_truncated,
    )


def _plist(value: object) -> str:
    return plistlib.dumps(value).decode("utf-8")


class FakeRunner:
    def __init__(self, results: list[ProcessResult]) -> None:
        self._results = list(results)
        self.calls: list[tuple[str, ...]] = []
        self.limits: list[ExecutionLimits | None] = []

    def run(self, argv, *, cwd=None, env_overrides=None, limits=None):
        self.calls.append(tuple(argv))
        self.limits.append(limits)
        return self._results.pop(0)


def test_host_info_uses_pinned_native_tools() -> None:
    runner = FakeRunner([_result(stdout="15.6.1\n"), _result(stdout="arm64\n")])

    info = MacOSInspector(runner=runner, system_name="Darwin").host_info()

    assert info.as_dict() == {"product_version": "15.6.1", "architecture": "arm64"}
    assert runner.calls == [
        ("/usr/bin/sw_vers", "-productVersion"),
        ("/usr/bin/uname", "-m"),
    ]


def test_host_info_uses_one_whole_operation_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = FakeRunner([_result(stdout="15.6.1\n"), _result(stdout="arm64\n")])
    ticks = iter((100.0, 101.0, 104.0))
    monkeypatch.setattr(inspection_module.time, "monotonic", lambda: next(ticks))

    MacOSInspector(runner=runner, system_name="Darwin").host_info(
        limits=ExecutionLimits(timeout_seconds=10.0)
    )

    assert [limit.timeout_seconds for limit in runner.limits if limit is not None] == [9.0, 6.0]


def test_host_info_fails_before_second_process_when_budget_is_exhausted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = FakeRunner([_result(stdout="15.6.1\n")])
    ticks = iter((100.0, 101.0, 110.0))
    monkeypatch.setattr(inspection_module.time, "monotonic", lambda: next(ticks))

    with pytest.raises(MacOSInspectionError, match="whole-operation timeout"):
        MacOSInspector(runner=runner, system_name="Darwin").host_info(
            limits=ExecutionLimits(timeout_seconds=10.0)
        )

    assert runner.calls == [("/usr/bin/sw_vers", "-productVersion")]


def test_usb_devices_use_io_usb_host_device_plist() -> None:
    payload = [
        {
            "USB Product Name": "USB2.0 Hub",
            "idVendor": 0x05E3,
            "idProduct": 0x0608,
            "IORegistryEntryLocation": "01100000",
            "IORegistryEntryChildren": [
                {
                    "USB Product Name": "USB Serial",
                    "idVendor": 0x1A86,
                    "idProduct": 0x7523,
                    "IORegistryEntryLocation": "01120000",
                },
                {
                    "kUSBProductString": "USB JTAG/serial debug unit",
                    "idVendor": 0x303A,
                    "idProduct": 0x1001,
                    "USB Serial Number": "40:4C:CA:5D:01:D8",
                    "locationID": 0x01130000,
                },
            ],
        }
    ]
    runner = FakeRunner([_result(stdout=_plist(payload))])

    devices = MacOSInspector(runner=runner, system_name="Darwin").usb_devices()

    assert [device.as_dict() for device in devices] == [
        {
            "name": "USB2.0 Hub",
            "vendor_id": "0x05e3",
            "product_id": "0x0608",
            "serial_number": None,
            "location_id": "0x01100000",
        },
        {
            "name": "USB Serial",
            "vendor_id": "0x1a86",
            "product_id": "0x7523",
            "serial_number": None,
            "location_id": "0x01120000",
        },
        {
            "name": "USB JTAG/serial debug unit",
            "vendor_id": "0x303a",
            "product_id": "0x1001",
            "serial_number": "40:4C:CA:5D:01:D8",
            "location_id": "0x01130000",
        },
    ]
    assert runner.calls == [("/usr/sbin/ioreg", "-r", "-c", "IOUSBHostDevice", "-a")]


def test_usb_devices_deduplicate_identity_and_ignore_invalid_rows() -> None:
    device = {
        "USB Product Name": "Debug Adapter",
        "idVendor": 0x1234,
        "idProduct": 0x5678,
        "locationID": 0x00100000,
    }
    payload = [
        {
            "IORegistryEntryChildren": [
                device,
                dict(device),
                {"USB Product Name": "Missing product", "idVendor": 0x1234},
                {"USB Product Name": "Invalid vendor", "idVendor": -1, "idProduct": 1},
                {"idVendor": 1, "idProduct": 2},
                "ignored",
            ]
        }
    ]
    runner = FakeRunner([_result(stdout=_plist(payload))])

    devices = MacOSInspector(runner=runner, system_name="Darwin").usb_devices()

    assert len(devices) == 1
    assert devices[0].name == "Debug Adapter"
    assert devices[0].vendor_id == "0x1234"
    assert devices[0].product_id == "0x5678"
    assert devices[0].location_id == "0x00100000"


def test_usb_devices_allow_missing_optional_location_and_serial() -> None:
    payload = [
        {
            "USB Product Name": "Minimal USB device",
            "idVendor": 1,
            "idProduct": 2,
        }
    ]
    runner = FakeRunner([_result(stdout=_plist(payload))])

    devices = MacOSInspector(runner=runner, system_name="Darwin").usb_devices()

    assert devices[0].vendor_id == "0x0001"
    assert devices[0].product_id == "0x0002"
    assert devices[0].serial_number is None
    assert devices[0].location_id is None


def test_serial_ports_use_ioreg_plist_and_deduplicate_callouts() -> None:
    payload = [
        {
            "IOCalloutDevice": "/dev/cu.usbserial-1",
            "IODialinDevice": "/dev/tty.usbserial-1",
            "IOTTYDevice": "usbserial-1",
        },
        {"IOCalloutDevice": "/dev/cu.usbserial-1"},
    ]
    runner = FakeRunner([_result(stdout=_plist(payload))])

    ports = MacOSInspector(runner=runner, system_name="Darwin").serial_ports()

    assert [port.as_dict() for port in ports] == [
        {
            "callout_device": "/dev/cu.usbserial-1",
            "dialin_device": "/dev/tty.usbserial-1",
            "tty_device": "usbserial-1",
        }
    ]


def test_serial_ports_ignore_invalid_rows() -> None:
    runner = FakeRunner([_result(stdout=_plist(["bad", {}, {"IOCalloutDevice": ""}]))])

    assert MacOSInspector(runner=runner, system_name="Darwin").serial_ports() == ()


def test_external_storage_inspects_each_diskutil_identifier() -> None:
    listing = {"AllDisks": ["disk4", "disk4s1", "not-a-disk"]}
    disk = {
        "DeviceNode": "/dev/disk4",
        "Whole": True,
        "MediaName": "SD Card Reader Media",
        "Protocol": "USB",
        "DiskSize": 32_000_000_000,
        "Internal": False,
        "Ejectable": True,
        "RemovableMedia": True,
        "ReadOnlyMedia": False,
    }
    volume = {
        "DeviceNode": "/dev/disk4s1",
        "Whole": False,
        "PartOfWhole": "disk4",
        "VolumeName": "FIRMWARE",
        "MountPoint": "/Volumes/FIRMWARE",
        "FileSystemPersonality": "MS-DOS FAT32",
        "DiskSize": 31_900_000_000,
        "Internal": False,
        "ReadOnlyVolume": False,
    }
    runner = FakeRunner(
        [
            _result(stdout=_plist(listing)),
            _result(stdout=_plist(disk)),
            _result(stdout=_plist(volume)),
        ]
    )

    devices = MacOSInspector(runner=runner, system_name="Darwin").external_storage()

    assert [device.identifier for device in devices] == ["disk4", "disk4s1"]
    assert devices[0].removable_media is True
    assert devices[1].mount_point == "/Volumes/FIRMWARE"
    assert devices[1].filesystem == "MS-DOS FAT32"
    assert runner.calls == [
        ("/usr/sbin/diskutil", "list", "-plist", "external", "physical"),
        ("/usr/sbin/diskutil", "info", "-plist", "disk4"),
        ("/usr/sbin/diskutil", "info", "-plist", "disk4s1"),
    ]


def test_external_storage_shares_budget_across_listing_and_info_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    listing = {"AllDisks": ["disk4", "disk4s1"]}
    disk = {"Whole": True, "Internal": False}
    volume = {"Whole": False, "PartOfWhole": "disk4", "Internal": False}
    runner = FakeRunner(
        [
            _result(stdout=_plist(listing)),
            _result(stdout=_plist(disk)),
            _result(stdout=_plist(volume)),
        ]
    )
    ticks = iter((100.0, 101.0, 104.0, 108.0))
    monkeypatch.setattr(inspection_module.time, "monotonic", lambda: next(ticks))

    MacOSInspector(runner=runner, system_name="Darwin").external_storage(
        limits=ExecutionLimits(timeout_seconds=10.0)
    )

    assert [limit.timeout_seconds for limit in runner.limits if limit is not None] == [
        9.0,
        6.0,
        2.0,
    ]


def test_external_storage_rejects_invalid_listing_shapes() -> None:
    runner = FakeRunner([_result(stdout=_plist([]))])
    with pytest.raises(MacOSInspectionError, match="unexpected diskutil schema"):
        MacOSInspector(runner=runner, system_name="Darwin").external_storage()

    runner = FakeRunner([_result(stdout=_plist({"AllDisks": "disk4"}))])
    with pytest.raises(MacOSInspectionError, match="missing AllDisks list"):
        MacOSInspector(runner=runner, system_name="Darwin").external_storage()


def test_external_storage_rejects_invalid_info_shape() -> None:
    runner = FakeRunner(
        [
            _result(stdout=_plist({"AllDisks": ["disk4"]})),
            _result(stdout=_plist([])),
        ]
    )

    with pytest.raises(MacOSInspectionError, match="unexpected diskutil schema"):
        MacOSInspector(runner=runner, system_name="Darwin").external_storage()


def test_non_macos_host_fails_closed() -> None:
    with pytest.raises(MacOSInspectionError, match="requires Darwin"):
        MacOSInspector(runner=FakeRunner([]), system_name="Linux").usb_devices()


def test_invalid_native_output_fails_closed() -> None:
    runner = FakeRunner([_result(stdout="not-plist")])

    with pytest.raises(MacOSInspectionError, match="invalid plist"):
        MacOSInspector(runner=runner, system_name="Darwin").usb_devices()


def test_usb_rejects_wrong_ioreg_schema() -> None:
    runner = FakeRunner([_result(stdout=_plist({}))])
    with pytest.raises(MacOSInspectionError, match="unexpected ioreg schema"):
        MacOSInspector(runner=runner, system_name="Darwin").usb_devices()


def test_serial_rejects_invalid_plist_and_schema() -> None:
    runner = FakeRunner([_result(stdout="not-plist")])
    with pytest.raises(MacOSInspectionError, match="invalid plist"):
        MacOSInspector(runner=runner, system_name="Darwin").serial_ports()

    runner = FakeRunner([_result(stdout=_plist({}))])
    with pytest.raises(MacOSInspectionError, match="unexpected ioreg schema"):
        MacOSInspector(runner=runner, system_name="Darwin").serial_ports()


def test_host_info_rejects_multiline_output() -> None:
    runner = FakeRunner([_result(stdout="15.6.1\nextra\n")])
    with pytest.raises(MacOSInspectionError, match="unexpected command output"):
        MacOSInspector(runner=runner, system_name="Darwin").host_info()


def test_failed_or_truncated_native_commands_fail_closed() -> None:
    runner = FakeRunner([_result(stderr="boom", exit_code=2)])
    with pytest.raises(MacOSInspectionError, match="boom"):
        MacOSInspector(runner=runner, system_name="Darwin").usb_devices()

    runner = FakeRunner([_result(stdout=_plist([]), stdout_truncated=True)])
    with pytest.raises(MacOSInspectionError, match="stdout was truncated"):
        MacOSInspector(runner=runner, system_name="Darwin").usb_devices()
