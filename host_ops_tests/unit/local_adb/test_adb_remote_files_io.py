from __future__ import annotations

from pathlib import Path

import pytest

from local_agent.host_ops.capabilities.local.adb.remote_files import (
    AdbRemoteFiles,
    AdbTransferError,
    normalize_remote_file_path,
)
from local_agent.host_ops.core.execution import ExecutionLimits, ProcessResult, ProcessState

ADB = "/opt/android/platform-tools/adb"
SERIAL = "ABC123"
DIGEST = "a" * 64


class FakeRunner:
    def __init__(self, *results: ProcessResult) -> None:
        self.results = list(results)
        self.commands: list[tuple[str, ...]] = []

    def run(self, command, *, limits=None):
        self.commands.append(tuple(command))
        return self.results.pop(0)


def _result(
    *,
    stdout: str = "",
    stderr: str = "",
    exit_code: int = 0,
    state: ProcessState = ProcessState.COMPLETED,
    stdout_truncated: bool = False,
) -> ProcessResult:
    return ProcessResult(
        state=state,
        exit_code=exit_code,
        stdout=stdout,
        stderr=stderr,
        duration_seconds=0.01,
        stdout_truncated=stdout_truncated,
    )


def _remote(runner: FakeRunner, provider=None) -> AdbRemoteFiles:
    limits = provider or (lambda: ExecutionLimits(timeout_seconds=5.0))
    return AdbRemoteFiles(runner, ADB, SERIAL, limits)


def test_push_and_pull_use_explicit_serial_and_literal_paths(tmp_path: Path) -> None:
    runner = FakeRunner(_result(), _result())
    remote = _remote(runner)
    source = tmp_path / "input.bin"
    source.write_bytes(b"payload")
    destination = tmp_path / "out.bin"

    remote.push(source, "/sdcard/input.bin")
    remote.pull("/sdcard/out.bin", destination)

    assert runner.commands == [
        (ADB, "-s", SERIAL, "push", str(source), "/sdcard/input.bin"),
        (ADB, "-s", SERIAL, "pull", "/sdcard/out.bin", str(destination)),
    ]


def test_push_and_pull_propagate_bounded_process_failures(tmp_path: Path) -> None:
    source = tmp_path / "input.bin"
    source.write_bytes(b"payload")

    with pytest.raises(AdbTransferError, match="truncated"):
        _remote(FakeRunner(_result(stdout_truncated=True))).push(source, "/sdcard/input.bin")

    with pytest.raises(AdbTransferError, match="ADB pull"):
        _remote(FakeRunner(_result(stderr="permission denied", exit_code=1))).pull(
            "/data/private.bin",
            tmp_path / "out.bin",
        )



def test_ready_directory_and_destination_checks_fail_closed() -> None:
    _remote(FakeRunner(_result(stdout="device\n"))).require_ready()

    with pytest.raises(AdbTransferError, match="not ready"):
        _remote(FakeRunner(_result(stdout="offline\n"))).require_ready()

    with pytest.raises(AdbTransferError, match="symbolic link"):
        _remote(FakeRunner(_result())).require_directory("/sdcard/tmp")

    with pytest.raises(AdbTransferError, match="does not exist"):
        _remote(
            FakeRunner(
                _result(exit_code=1),
                _result(exit_code=1),
            )
        ).require_directory("/sdcard/tmp")

    assert _remote(
        FakeRunner(
            _result(exit_code=1),
            _result(exit_code=1),
        )
    ).validate_destination("/sdcard/file.bin", replace=False) is False

    with pytest.raises(AdbTransferError, match="replace intent"):
        _remote(
            FakeRunner(
                _result(exit_code=1),
                _result(),
                _result(),
            )
        ).validate_destination("/sdcard/file.bin", replace=False)


def test_size_and_sha256_use_fixed_literal_commands() -> None:
    runner = FakeRunner(
        _result(stdout="7 /sdcard/file.bin\n"),
        _result(stdout=f"{DIGEST}  /sdcard/file.bin\n"),
    )
    remote = _remote(runner)

    assert remote.size("/sdcard/file.bin") == 7
    assert remote.sha256("/sdcard/file.bin") == DIGEST
    assert runner.commands == [
        (ADB, "-s", SERIAL, "shell", "wc", "-c", "/sdcard/file.bin"),
        (ADB, "-s", SERIAL, "shell", "sha256sum", "/sdcard/file.bin"),
    ]


def test_sha256_falls_back_to_toybox_without_remote_shell_script() -> None:
    runner = FakeRunner(
        _result(stderr="sha256sum unavailable", exit_code=127),
        _result(stdout=f"{DIGEST}  /sdcard/file.bin\n"),
    )

    assert _remote(runner).sha256("/sdcard/file.bin") == DIGEST
    assert runner.commands == [
        (ADB, "-s", SERIAL, "shell", "sha256sum", "/sdcard/file.bin"),
        (ADB, "-s", SERIAL, "shell", "toybox", "sha256sum", "/sdcard/file.bin"),
    ]


def test_sha256_fails_closed_for_missing_or_malformed_hash_tools() -> None:
    with pytest.raises(AdbTransferError, match="no supported hash command"):
        _remote(
            FakeRunner(
                _result(exit_code=127),
                _result(exit_code=127),
                _result(exit_code=127),
            )
        ).sha256("/sdcard/file.bin")

    with pytest.raises(AdbTransferError, match="invalid output"):
        _remote(FakeRunner(_result(stdout="not-a-digest\n"))).sha256("/sdcard/file.bin")


def test_commit_stage_and_cleanup_keep_exact_remote_paths() -> None:
    runner = FakeRunner(_result(), _result(exit_code=1))
    remote = _remote(runner)

    remote.commit_stage(
        "/sdcard/.hostops.tmp",
        "/sdcard/file.bin",
        replace=False,
    )

    assert runner.commands == [
        (
            ADB,
            "-s",
            SERIAL,
            "shell",
            "mv",
            "-nT",
            "/sdcard/.hostops.tmp",
            "/sdcard/file.bin",
        ),
        (ADB, "-s", SERIAL, "shell", "test", "-e", "/sdcard/.hostops.tmp"),
    ]

    assert _remote(FakeRunner(_result())).cleanup("/sdcard/.hostops.tmp") is True


@pytest.mark.parametrize(
    "value",
    [
        "relative.bin",
        "/",
        "/sdcard/../data/file.bin",
        "/sdcard/file name.bin",
        "/sdcard//file.bin",
        "//sdcard/file.bin",
    ],
)
def test_remote_path_validation_rejects_nonliteral_or_ambiguous_paths(value: str) -> None:
    with pytest.raises(AdbTransferError):
        normalize_remote_file_path(value)


def test_remote_process_timeout_or_truncation_fails_closed() -> None:
    with pytest.raises(AdbTransferError, match="truncated"):
        _remote(FakeRunner(_result(stdout="device\n", stdout_truncated=True))).require_ready()

    with pytest.raises(AdbTransferError, match="remote test"):
        _remote(
            FakeRunner(
                _result(
                    state=ProcessState.TIMED_OUT,
                    exit_code=None,
                    stderr="timeout",
                )
            )
        ).require_directory("/sdcard/tmp")


def test_cleanup_handles_exhausted_budget_in_best_effort_mode() -> None:
    def exhausted() -> ExecutionLimits:
        raise AdbTransferError("deadline exhausted")

    remote = _remote(FakeRunner(), exhausted)

    assert remote.cleanup("/sdcard/.tmp", best_effort=True) is False
    with pytest.raises(AdbTransferError, match="deadline exhausted"):
        remote.cleanup("/sdcard/.tmp")


def test_remote_test_exit_one_with_diagnostics_is_not_treated_as_false() -> None:
    remote = _remote(FakeRunner(_result(stderr="device disconnected", exit_code=1)))

    with pytest.raises(AdbTransferError, match="device disconnected"):
        remote.require_directory("/sdcard")
