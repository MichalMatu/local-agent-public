from __future__ import annotations

import pytest

from local_agent.host_ops.capabilities.local.adb import AdbClient, AdbInspectionError
from local_agent.host_ops.core.execution import ProcessResult, ProcessState

ADB = "/opt/android/platform-tools/adb"
SERIAL = "ABC123"


class FakeRunner:
    def __init__(self, *results: ProcessResult) -> None:
        self.results = list(results)
        self.commands: list[tuple[str, ...]] = []

    def run(self, command, *, limits=None):
        self.commands.append(tuple(command))
        return self.results.pop(0)


def _result(
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
    return ADB


def test_logcat_preflights_device_and_captures_bounded_tail() -> None:
    runner = FakeRunner(
        _result("device\n"),
        _result("line one\nline two\n"),
    )

    result = AdbClient(runner, resolver=_resolver).logcat(SERIAL, lines=25)

    assert result.serial == SERIAL
    assert result.lines_requested == 25
    assert result.line_count == 2
    assert result.text == "line one\nline two\n"
    assert runner.commands == [
        (ADB, "-s", SERIAL, "get-state"),
        (ADB, "-s", SERIAL, "logcat", "-d", "-t", "25"),
    ]


def test_logcat_rejects_non_ready_device_before_capture() -> None:
    runner = FakeRunner(_result("offline\n"))

    with pytest.raises(AdbInspectionError, match="not ready"):
        AdbClient(runner, resolver=_resolver).logcat(SERIAL)

    assert len(runner.commands) == 1


def test_logcat_rejects_invalid_line_limit_before_execution() -> None:
    runner = FakeRunner()

    for invalid in (0, 10_001, True):
        with pytest.raises(AdbInspectionError, match="lines"):
            AdbClient(runner, resolver=_resolver).logcat(SERIAL, lines=invalid)

    assert runner.commands == []


def test_logcat_rejects_truncated_capture() -> None:
    runner = FakeRunner(
        _result("device\n"),
        _result("partial\n", stdout_truncated=True),
    )

    with pytest.raises(AdbInspectionError, match="truncated"):
        AdbClient(runner, resolver=_resolver).logcat(SERIAL, lines=10)


def test_logcat_rejects_invalid_serial_before_execution() -> None:
    runner = FakeRunner()

    with pytest.raises(AdbInspectionError, match="serial"):
        AdbClient(runner, resolver=_resolver).logcat("--bad")

    assert runner.commands == []
