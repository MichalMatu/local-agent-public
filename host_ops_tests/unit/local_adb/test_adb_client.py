from __future__ import annotations

import pytest

from local_agent.host_ops.capabilities.local.adb import AdbClient, AdbInspectionError
from local_agent.host_ops.core.execution import ExecutionLimits, ProcessResult, ProcessState


class FakeRunner:
    def __init__(self, *results: ProcessResult) -> None:
        self.results = list(results)
        self.commands: list[tuple[str, ...]] = []
        self.limits: list[ExecutionLimits | None] = []

    def run(self, command, *, limits=None):
        self.commands.append(tuple(command))
        self.limits.append(limits)
        return self.results.pop(0)


def _completed(
    stdout: str = "",
    *,
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


def _resolver(name: str) -> str | None:
    assert name == "adb"
    return "/opt/android/platform-tools/adb"


def test_devices_parses_ready_and_non_ready_devices() -> None:
    runner = FakeRunner(
        _completed(
            "List of devices attached\n"
            "R58N123ABC device usb:1-1 product:dreamlte model:SM_G950F "
            "device:dreamlte transport_id:1\n"
            "emulator-5554 offline transport_id:2\n"
            "10.0.0.8:5555 unauthorized product:test model:Pixel_8 transport_id:3\n"
        )
    )

    devices = AdbClient(runner, resolver=_resolver).devices()

    assert [item.serial for item in devices] == ["R58N123ABC", "emulator-5554", "10.0.0.8:5555"]
    assert devices[0].state == "device"
    assert devices[0].model == "SM_G950F"
    assert devices[0].usb == "1-1"
    assert devices[1].state == "offline"
    assert devices[2].state == "unauthorized"
    assert runner.commands == [("/opt/android/platform-tools/adb", "devices", "-l")]


def test_devices_accepts_empty_listing() -> None:
    runner = FakeRunner(_completed("List of devices attached\n\n"))

    assert AdbClient(runner, resolver=_resolver).devices() == ()


def test_devices_rejects_unexpected_header() -> None:
    runner = FakeRunner(_completed("unexpected\n"))

    with pytest.raises(AdbInspectionError, match="unexpected header"):
        AdbClient(runner, resolver=_resolver).devices()


def test_devices_rejects_duplicate_serial() -> None:
    runner = FakeRunner(
        _completed("List of devices attached\nABC device\nABC device transport_id:2\n")
    )

    with pytest.raises(AdbInspectionError, match="duplicate serial"):
        AdbClient(runner, resolver=_resolver).devices()


def test_devices_reports_missing_adb() -> None:
    with pytest.raises(AdbInspectionError, match="not found"):
        AdbClient(FakeRunner(), resolver=lambda _name: None).devices()


def test_devices_rejects_truncated_output() -> None:
    runner = FakeRunner(_completed("List of devices attached\nABC device\n", stdout_truncated=True))

    with pytest.raises(AdbInspectionError, match="truncated"):
        AdbClient(runner, resolver=_resolver).devices()


def test_identity_requires_ready_explicit_serial_and_reads_only_fixed_properties() -> None:
    runner = FakeRunner(
        _completed("List of devices attached\nR58N123ABC device model:SM_S906B transport_id:1\n"),
        _completed("samsung\n"),
        _completed("SM-S906B\n"),
        _completed("g0sxeea\n"),
        _completed("g0s\n"),
        _completed("16\n"),
        _completed("36\n"),
        _completed("samsung/g0sxeea/g0s:16/BUILD:user/release-keys\n"),
    )

    identity = AdbClient(runner, resolver=_resolver).identity("R58N123ABC")

    assert identity.serial == "R58N123ABC"
    assert identity.state == "device"
    assert identity.manufacturer == "samsung"
    assert identity.model == "SM-S906B"
    assert identity.android_version == "16"
    assert identity.sdk_level == "36"
    assert runner.commands[1:] == [
        (
            "/opt/android/platform-tools/adb",
            "-s",
            "R58N123ABC",
            "shell",
            "getprop",
            property_name,
        )
        for property_name in (
            "ro.product.manufacturer",
            "ro.product.model",
            "ro.product.name",
            "ro.product.device",
            "ro.build.version.release",
            "ro.build.version.sdk",
            "ro.build.fingerprint",
        )
    ]


def test_identity_rejects_non_ready_device_before_getprop() -> None:
    runner = FakeRunner(_completed("List of devices attached\nABC unauthorized transport_id:1\n"))

    with pytest.raises(AdbInspectionError, match="not ready"):
        AdbClient(runner, resolver=_resolver).identity("ABC")

    assert len(runner.commands) == 1


def test_identity_rejects_unknown_device() -> None:
    runner = FakeRunner(_completed("List of devices attached\nOTHER device transport_id:1\n"))

    with pytest.raises(AdbInspectionError, match="not found"):
        AdbClient(runner, resolver=_resolver).identity("ABC")


def test_identity_rejects_invalid_serial_before_running_adb() -> None:
    runner = FakeRunner()

    with pytest.raises(AdbInspectionError, match="serial"):
        AdbClient(runner, resolver=_resolver).identity("--transport-id=1")

    assert runner.commands == []


def test_identity_rejects_multiline_target_property_output() -> None:
    runner = FakeRunner(
        _completed("List of devices attached\nABC device transport_id:1\n"),
        _completed("samsung\nunexpected-second-line\n"),
    )

    with pytest.raises(AdbInspectionError, match="multiline"):
        AdbClient(runner, resolver=_resolver).identity("ABC")

    assert runner.commands[-1][-1] == "ro.product.manufacturer"
