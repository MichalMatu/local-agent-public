"""Native subprocess acceptance for the bounded raw-byte ProcessRunner sink."""

from __future__ import annotations

import errno
import os
import sys
from pathlib import Path

import pytest

from local_agent.host_ops.core.execution import ExecutionLimits, ProcessRunner, ProcessState
from local_agent.host_ops.core.execution import process as process_module


def _stage(tmp_path: Path) -> tuple[Path, int]:
    path = tmp_path / "exclusive-stage.bin"
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    return path, descriptor


@pytest.mark.parametrize("count", [0, 32, 33])
def test_stream_enforces_cap_before_cleanup(tmp_path: Path, count: int) -> None:
    path, descriptor = _stage(tmp_path)
    try:
        result = ProcessRunner().run(
            (
                sys.executable, "-c",
                f"import sys; sys.stdout.buffer.write(b'x' * {count}); sys.stdout.flush()",
            ),
            binary_stdout_fd=descriptor,
            binary_stdout_limit=32,
            limits=ExecutionLimits(timeout_seconds=5.0),
        )
        os.fsync(descriptor)
        os.fstat(descriptor)  # The caller, not the runner, owns the sink FD.
    finally:
        os.close(descriptor)
    assert path.stat().st_size == min(count, 32)
    assert path.read_bytes() == b"x" * min(count, 32)
    assert result.stdout == ""
    if count == 33:
        assert result.state is ProcessState.OUTPUT_LIMIT_EXCEEDED
        assert not result.ok
    else:
        assert result.ok


def test_full_byte_range_is_written_without_utf8_conversion(tmp_path: Path) -> None:
    path, descriptor = _stage(tmp_path)
    try:
        result = ProcessRunner().run(
            (sys.executable, "-c", "import sys; sys.stdout.buffer.write(bytes(range(256)))"),
            binary_stdout_fd=descriptor,
            binary_stdout_limit=256,
            limits=ExecutionLimits(timeout_seconds=5.0),
        )
    finally:
        os.close(descriptor)
    assert result.ok
    assert result.stdout == ""
    assert path.read_bytes() == bytes(range(256))


def test_overflow_terminates_owned_process_and_bounds_stderr(tmp_path: Path) -> None:
    path, descriptor = _stage(tmp_path)
    try:
        result = ProcessRunner().run(
            (
                sys.executable, "-c",
                "import sys,time; "
                "sys.stderr.write('e' * 100000); sys.stderr.flush(); "
                "sys.stdout.buffer.write(b'X' * 1024); sys.stdout.flush(); "
                "time.sleep(20)",
            ),
            binary_stdout_fd=descriptor,
            binary_stdout_limit=4,
            limits=ExecutionLimits(
                timeout_seconds=5.0,
                terminate_grace_seconds=0.1,
                pipe_drain_seconds=0.1,
                max_stderr_bytes=16,
            ),
        )
    finally:
        os.close(descriptor)
    assert result.state is ProcessState.OUTPUT_LIMIT_EXCEEDED
    assert path.stat().st_size == 4
    assert result.stderr_truncated
    assert len(result.stderr.encode()) <= 16
    assert result.duration_seconds < 5.0


def test_partial_binary_output_does_not_hide_remote_nonzero_exit(tmp_path: Path) -> None:
    path, descriptor = _stage(tmp_path)
    try:
        result = ProcessRunner().run(
            (
                sys.executable, "-c",
                "import sys;sys.stdout.buffer.write(b'abc');sys.stdout.flush();sys.exit(7)",
            ),
            binary_stdout_fd=descriptor,
            binary_stdout_limit=8,
            limits=ExecutionLimits(timeout_seconds=5.0),
        )
    finally:
        os.close(descriptor)
    assert result.state is ProcessState.COMPLETED
    assert result.exit_code == 7
    assert not result.ok
    assert path.read_bytes() == b"abc"


def test_binary_sink_write_error_is_not_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    path, descriptor = _stage(tmp_path)
    real_write = os.write

    def no_space(fd: int, data: bytes) -> int:
        if fd == descriptor:
            raise OSError(errno.ENOSPC, "simulated full disk")
        return real_write(fd, data)

    monkeypatch.setattr(process_module.os, "write", no_space)
    try:
        result = ProcessRunner().run(
            (sys.executable, "-c", "import sys;sys.stdout.buffer.write(b'abcd')"),
            binary_stdout_fd=descriptor,
            binary_stdout_limit=16,
            limits=ExecutionLimits(timeout_seconds=5.0),
        )
    finally:
        os.close(descriptor)
    assert path.stat().st_size == 0
    assert result.state is ProcessState.OUTPUT_SINK_FAILED
    assert "ENOSPC" in result.error or "No space" in result.error or "full disk" in result.error


@pytest.mark.skipif(os.name != "posix", reason="POSIX process groups required")
def test_nested_host_lease_keeps_outer_process_group(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    lease_path = tmp_path / "host-resource.lock"
    path, descriptor = _stage(tmp_path)
    parent_group = os.getpgrp()
    monkeypatch.delenv("LOCAL_AGENT_LEASE_FDS", raising=False)
    monkeypatch.delenv("LOCAL_AGENT_LEASE_KEYS_DIGEST", raising=False)
    with lease_path.open("a+") as lease:
        monkeypatch.setenv("LOCAL_AGENT_RESOURCE_LEASE_FDS", str(lease.fileno()))
        try:
            result = ProcessRunner().run(
                (
                    sys.executable, "-c",
                    "import sys,time;sys.stdout.buffer.write(b'x'*1024);"
                    "sys.stdout.flush();time.sleep(20)",
                ),
                binary_stdout_fd=descriptor,
                binary_stdout_limit=4,
                limits=ExecutionLimits(
                    timeout_seconds=5.0,
                    terminate_grace_seconds=0.1,
                    pipe_drain_seconds=0.1,
                ),
            )
        finally:
            os.close(descriptor)
        assert os.fstat(lease.fileno())
    assert result.state is ProcessState.OUTPUT_LIMIT_EXCEEDED
    assert os.getpgrp() == parent_group
    assert path.stat().st_size == 4


@pytest.mark.parametrize("mode", ["nonempty", "offset"])
def test_binary_sink_rejects_non_private_stage_before_spawn(
    tmp_path: Path, mode: str,
) -> None:
    path, descriptor = _stage(tmp_path)
    try:
        if mode == "nonempty":
            os.write(descriptor, b"prior user data")
        else:
            os.lseek(descriptor, 1, os.SEEK_SET)
        with pytest.raises(ValueError, match="empty stage at offset zero"):
            ProcessRunner().run(
                (sys.executable, "-c", "import sys; sys.stdout.buffer.write(b'x')"),
                binary_stdout_fd=descriptor,
                binary_stdout_limit=16,
            )
    finally:
        os.close(descriptor)
    if mode == "nonempty":
        assert path.read_bytes() == b"prior user data"
    else:
        assert path.stat().st_size == 0


@pytest.mark.skipif(os.name != "posix", reason="nested process context requires POSIX")
def test_binary_limit_reports_possible_lingering_descendants(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A grandchild retains stdout/stderr after the main child exits. Nested
    # Host Ops cannot signal its supervisor's process group to kill it.
    stage, descriptor = _stage(tmp_path)
    parent_group = os.getpgrp()
    with (tmp_path / "host-resource.lock").open("a+") as lease:
        monkeypatch.setenv("LOCAL_AGENT_RESOURCE_LEASE_FDS", str(lease.fileno()))
        monkeypatch.delenv("LOCAL_AGENT_LEASE_FDS", raising=False)
        monkeypatch.delenv("LOCAL_AGENT_LEASE_KEYS_DIGEST", raising=False)
        child = (
            "import subprocess,sys;"
            "subprocess.Popen([sys.executable,'-c','import time;time.sleep(1.5)'],"
            "stdout=sys.stdout,stderr=sys.stderr);"
            "sys.stdout.buffer.write(b'X'*1024);sys.stdout.flush()"
        )
        try:
            result = ProcessRunner().run(
                (sys.executable, "-c", child),
                binary_stdout_fd=descriptor,
                binary_stdout_limit=4,
                limits=ExecutionLimits(
                    timeout_seconds=5.0,
                    terminate_grace_seconds=0.1,
                    pipe_drain_seconds=0.1,
                ),
            )
        finally:
            os.close(descriptor)
    assert result.state is ProcessState.OUTPUT_LIMIT_EXCEEDED
    assert result.error is not None
    assert "descendants may still be running" in result.error
    assert os.getpgrp() == parent_group
    assert stage.read_bytes() == b"XXXX"
