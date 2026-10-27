from __future__ import annotations

import importlib
import plistlib

import pytest

from local_agent.host_ops.capabilities.local.macos import MacOSStorageControlError, MacOSStorageController
from local_agent.host_ops.core.execution import ExecutionLimits, ProcessResult, ProcessState

storage_module = importlib.import_module(
    "local_agent.host_ops.capabilities.local.macos.storage"
)


def _result(*, stdout: str = "", stderr: str = "", exit_code: int = 0) -> ProcessResult:
    return ProcessResult(
        state=ProcessState.COMPLETED,
        exit_code=exit_code,
        stdout=stdout,
        stderr=stderr,
        duration_seconds=0.01,
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


def test_mount_preflights_external_target() -> None:
    runner = FakeRunner(
        [
            _result(stdout=_plist({"Internal": False, "Whole": False})),
            _result(stdout="Volume FIRMWARE mounted\n"),
        ]
    )

    result = MacOSStorageController(runner=runner, system_name="Darwin").mount("disk4s1")

    assert result.as_dict() == {
        "action": "mount",
        "identifier": "disk4s1",
        "message": "Volume FIRMWARE mounted",
    }
    assert runner.calls == [
        ("/usr/sbin/diskutil", "info", "-plist", "disk4s1"),
        ("/usr/sbin/diskutil", "mount", "disk4s1"),
    ]


def test_mount_uses_one_whole_operation_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = FakeRunner(
        [
            _result(stdout=_plist({"Internal": False, "Whole": False})),
            _result(stdout="Volume FIRMWARE mounted\n"),
        ]
    )
    ticks = iter((100.0, 101.0, 105.0))
    monkeypatch.setattr(storage_module.time, "monotonic", lambda: next(ticks))

    MacOSStorageController(runner=runner, system_name="Darwin").mount(
        "disk4s1",
        limits=ExecutionLimits(timeout_seconds=10.0),
    )

    assert [limit.timeout_seconds for limit in runner.limits if limit is not None] == [9.0, 5.0]


def test_mount_fails_before_action_when_budget_is_exhausted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = FakeRunner([_result(stdout=_plist({"Internal": False, "Whole": False}))])
    ticks = iter((100.0, 101.0, 110.0))
    monkeypatch.setattr(storage_module.time, "monotonic", lambda: next(ticks))

    with pytest.raises(MacOSStorageControlError, match="whole-operation timeout") as captured:
        MacOSStorageController(runner=runner, system_name="Darwin").mount(
            "disk4s1",
            limits=ExecutionLimits(timeout_seconds=10.0),
        )

    assert captured.value.action_attempted is False
    assert runner.calls == [("/usr/sbin/diskutil", "info", "-plist", "disk4s1")]


def test_unmount_rejects_internal_target() -> None:
    runner = FakeRunner([_result(stdout=_plist({"Internal": True, "Whole": False}))])

    with pytest.raises(MacOSStorageControlError, match="not confirmed external"):
        MacOSStorageController(runner=runner, system_name="Darwin").unmount("disk0s1")

    assert len(runner.calls) == 1


def test_eject_requires_whole_external_disk() -> None:
    runner = FakeRunner([_result(stdout=_plist({"Internal": False, "Whole": False}))])

    with pytest.raises(MacOSStorageControlError, match="whole external disk"):
        MacOSStorageController(runner=runner, system_name="Darwin").eject("disk4s1")


def test_eject_whole_external_disk() -> None:
    runner = FakeRunner(
        [
            _result(stdout=_plist({"Internal": False, "Whole": True})),
            _result(stdout="Disk disk4 ejected\n"),
        ]
    )

    result = MacOSStorageController(runner=runner, system_name="Darwin").eject("disk4")

    assert result.action == "eject"
    assert runner.calls[-1] == ("/usr/sbin/diskutil", "eject", "disk4")


def test_storage_control_rejects_invalid_identifier_before_execution() -> None:
    runner = FakeRunner([])

    with pytest.raises(MacOSStorageControlError, match="diskN"):
        MacOSStorageController(runner=runner, system_name="Darwin").mount("/dev/disk4;rm")

    assert runner.calls == []


def test_storage_control_fails_closed_off_macos() -> None:
    runner = FakeRunner([])

    with pytest.raises(MacOSStorageControlError, match="requires Darwin"):
        MacOSStorageController(runner=runner, system_name="Linux").mount("disk4s1")


def test_storage_action_propagates_diskutil_failure() -> None:
    runner = FakeRunner(
        [
            _result(stdout=_plist({"Internal": False, "Whole": False})),
            _result(stderr="busy", exit_code=1),
        ]
    )

    with pytest.raises(MacOSStorageControlError, match="busy") as captured:
        MacOSStorageController(runner=runner, system_name="Darwin").unmount("disk4s1")

    assert captured.value.action_attempted is True
