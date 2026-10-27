"""Single owner for bounded local child-process lifecycle."""

from __future__ import annotations

import contextlib
import os
import re
import selectors
import signal
import stat
import subprocess
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import BinaryIO, cast

from .limits import ExecutionLimits
from .result import ProcessResult, ProcessState

_READ_CHUNK_BYTES = 64 * 1024
_POLL_INTERVAL_SECONDS = 0.05
_LOCAL_AGENT_LEASE_FDS_ENV = "LOCAL_AGENT_LEASE_FDS"
_LOCAL_AGENT_RESOURCE_LEASE_FDS_ENV = "LOCAL_AGENT_RESOURCE_LEASE_FDS"
_LOCAL_AGENT_LEASE_KEYS_DIGEST_ENV = "LOCAL_AGENT_LEASE_KEYS_DIGEST"
_LOCAL_AGENT_INTERNAL_ENV_NAMES = (
    _LOCAL_AGENT_LEASE_FDS_ENV,
    _LOCAL_AGENT_RESOURCE_LEASE_FDS_ENV,
    _LOCAL_AGENT_LEASE_KEYS_DIGEST_ENV,
)
_LEASE_DIGEST_PATTERN = re.compile(r"^[0-9a-f]{64}$")


class _BoundedBuffer:
    def __init__(self, limit: int) -> None:
        self._limit = limit
        self._buffer = bytearray()
        self.truncated = False

    @property
    def data(self) -> bytes:
        return bytes(self._buffer)

    def append(self, chunk: bytes) -> None:
        remaining = self._limit - len(self._buffer)
        if remaining > 0:
            self._buffer.extend(chunk[:remaining])
        if len(chunk) > max(remaining, 0):
            self.truncated = True

    def mark_incomplete(self) -> None:
        self.truncated = True


class _BinarySink:
    """Write binary child stdout to an already-open private regular file."""

    def __init__(self, descriptor: int, max_bytes: int) -> None:
        if type(descriptor) is not int or descriptor < 0:
            raise ValueError("binary_stdout_fd must be an open file descriptor")
        if type(max_bytes) is not int or max_bytes < 1:
            raise ValueError("binary_stdout_limit must be a positive integer")
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise ValueError("binary stdout sink must be a regular file")
        if metadata.st_size != 0 or os.lseek(descriptor, 0, os.SEEK_CUR) != 0:
            raise ValueError("binary stdout sink must be an empty stage at offset zero")
        self.descriptor = descriptor
        self.max_bytes = max_bytes
        self.written = 0
        self.overflowed = False
        self.write_error: str | None = None

    @property
    def failed(self) -> bool:
        return self.overflowed or self.write_error is not None

    def consume(self, chunk: bytes) -> None:
        if self.failed:
            return
        remaining = self.max_bytes - self.written
        if len(chunk) > remaining:
            self.overflowed = True
        bounded = memoryview(chunk)[:remaining]
        while bounded:
            try:
                count = os.write(self.descriptor, bounded)
            except InterruptedError:
                continue
            except OSError as exc:
                self.write_error = f"{type(exc).__name__}: {exc}"[:512]
                return
            if count <= 0:
                self.write_error = "binary stdout sink made no progress"
                return
            self.written += count
            bounded = bounded[count:]


class DetachedProcessSpawner:
    """Spawn one persistent detached process through the shared execution boundary."""

    def spawn(
        self,
        argv: Sequence[str],
        *,
        cwd: Path | None = None,
        env_overrides: Mapping[str, str] | None = None,
    ) -> int:
        if os.name != "posix":
            raise RuntimeError("DetachedProcessSpawner currently requires a POSIX host")
        normalized_argv = _normalize_argv(argv)
        environment = _environment_with_overrides(env_overrides)
        _validate_local_agent_context(environment)
        child_environment = _child_environment(environment)
        try:
            process = subprocess.Popen(
                normalized_argv,
                cwd=cwd,
                env=child_environment,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                close_fds=True,
                start_new_session=True,
            )
        except (OSError, ValueError) as exc:
            raise RuntimeError(
                f"detached process spawn failed: {type(exc).__name__}: {exc}"
            ) from exc
        return int(process.pid)


class ProcessRunner:
    """Execute argv without a shell and return normalized bounded evidence."""

    def run(
        self,
        argv: Sequence[str],
        *,
        cwd: Path | None = None,
        env_overrides: Mapping[str, str] | None = None,
        limits: ExecutionLimits | None = None,
        binary_stdout_fd: int | None = None,
        binary_stdout_limit: int | None = None,
    ) -> ProcessResult:
        if os.name != "posix":
            raise RuntimeError("ProcessRunner currently requires a POSIX host")

        normalized_argv = _normalize_argv(argv)
        applied_limits = limits or ExecutionLimits()
        if (binary_stdout_fd is None) != (binary_stdout_limit is None):
            raise ValueError("binary stdout sink and byte limit must be provided together")
        binary_sink = (
            _BinarySink(binary_stdout_fd, binary_stdout_limit)
            if binary_stdout_fd is not None and binary_stdout_limit is not None
            else None
        )
        environment = _environment_with_overrides(env_overrides)
        nested_under_local_agent = _validate_local_agent_context(environment)
        child_environment = _child_environment(environment)
        started_at = time.monotonic()

        try:
            process = subprocess.Popen(
                normalized_argv,
                cwd=cwd,
                env=child_environment,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=not nested_under_local_agent,
            )
        except (OSError, ValueError) as exc:
            return ProcessResult(
                state=ProcessState.SPAWN_FAILED,
                exit_code=None,
                stdout="",
                stderr="",
                duration_seconds=time.monotonic() - started_at,
                error=f"{type(exc).__name__}: {exc}",
            )

        stdout = cast(BinaryIO, process.stdout)
        stderr = cast(BinaryIO, process.stderr)
        stdout_buffer = _BoundedBuffer(applied_limits.max_stdout_bytes)
        stderr_buffer = _BoundedBuffer(applied_limits.max_stderr_bytes)
        timed_out, lingering_descendants = _drain_process(
            process,
            stdout,
            stderr,
            stdout_buffer,
            stderr_buffer,
            applied_limits,
            started_at,
            owns_process_group=not nested_under_local_agent,
            binary_sink=binary_sink,
        )

        state = ProcessState.COMPLETED
        error: str | None = None
        if binary_sink is not None and binary_sink.write_error is not None:
            state = ProcessState.OUTPUT_SINK_FAILED
            error = f"binary stdout sink failed: {binary_sink.write_error}"
        elif binary_sink is not None and binary_sink.overflowed:
            state = ProcessState.OUTPUT_LIMIT_EXCEEDED
            error = f"binary stdout exceeded {binary_sink.max_bytes} byte limit"
        elif timed_out:
            state = ProcessState.TIMED_OUT
            error = f"process exceeded {applied_limits.timeout_seconds:g}s timeout"
        elif lingering_descendants:
            state = ProcessState.LINGERING_DESCENDANTS
            error = "process exited while local descendants were still alive"

        if (
            binary_sink is not None
            and lingering_descendants
            and state is not ProcessState.LINGERING_DESCENDANTS
        ):
            error = f"{error}; child descendants may still be running"

        return ProcessResult(
            state=state,
            exit_code=process.returncode,
            stdout=stdout_buffer.data.decode("utf-8", errors="replace"),
            stderr=stderr_buffer.data.decode("utf-8", errors="replace"),
            duration_seconds=time.monotonic() - started_at,
            stdout_truncated=stdout_buffer.truncated,
            stderr_truncated=stderr_buffer.truncated,
            error=error,
        )


def _drain_process(
    process: subprocess.Popen[bytes],
    stdout: BinaryIO,
    stderr: BinaryIO,
    stdout_buffer: _BoundedBuffer,
    stderr_buffer: _BoundedBuffer,
    limits: ExecutionLimits,
    started_at: float,
    *,
    owns_process_group: bool,
    binary_sink: _BinarySink | None = None,
) -> tuple[bool, bool]:
    selector = selectors.DefaultSelector()
    stdout_descriptor = stdout.fileno()
    streams = {
        stdout_descriptor: (stdout, stdout_buffer),
        stderr.fileno(): (stderr, stderr_buffer),
    }
    for descriptor in streams:
        os.set_blocking(descriptor, False)
        selector.register(descriptor, selectors.EVENT_READ)

    timed_out = False
    lingering_descendants = False
    sink_abort_signaled = False
    process_deadline = started_at + limits.timeout_seconds
    drain_deadline: float | None = None
    term_deadline: float | None = None
    kill_deadline: float | None = None

    try:
        while True:
            now = time.monotonic()
            process_alive = process.poll() is None

            if (
                process_alive
                and not timed_out
                and not sink_abort_signaled
                and now >= process_deadline
            ):
                timed_out = True
                _signal_process_tree(process, signal.SIGTERM, owns_process_group)
                term_deadline = now + limits.terminate_grace_seconds

            process_alive = process.poll() is None
            if (
                process_alive
                and (timed_out or sink_abort_signaled)
                and term_deadline is not None
                and now >= term_deadline
            ):
                _signal_process_tree(process, signal.SIGKILL, owns_process_group)
                term_deadline = None

            process_alive = process.poll() is None
            if not process_alive and drain_deadline is None:
                drain_deadline = now + limits.pipe_drain_seconds

            group_alive = owns_process_group and _process_group_alive(process.pid)
            if (
                not process_alive
                and not lingering_descendants
                and drain_deadline is not None
                and now >= drain_deadline
                and (selector.get_map() or group_alive)
            ):
                lingering_descendants = True
                if owns_process_group:
                    _signal_process_group(process.pid, signal.SIGTERM)
                    term_deadline = now + limits.terminate_grace_seconds
                else:
                    _abandon_open_streams(selector, streams)
                    break

            group_alive = owns_process_group and _process_group_alive(process.pid)
            if (
                lingering_descendants
                and owns_process_group
                and term_deadline is not None
                and now >= term_deadline
            ):
                if group_alive:
                    _signal_process_group(process.pid, signal.SIGKILL)
                term_deadline = None
                kill_deadline = now + limits.terminate_grace_seconds

            group_alive = owns_process_group and _process_group_alive(process.pid)
            if (
                lingering_descendants
                and owns_process_group
                and kill_deadline is not None
                and now >= kill_deadline
                and (selector.get_map() or group_alive)
            ):
                _abandon_open_streams(selector, streams)
                break

            if not process_alive and not selector.get_map() and not group_alive:
                break

            if selector.get_map():
                events = selector.select(_POLL_INTERVAL_SECONDS)
                for key, _mask in events:
                    descriptor = key.fd
                    stream, buffer = streams[descriptor]
                    try:
                        chunk = os.read(descriptor, _READ_CHUNK_BYTES)
                    except BlockingIOError:
                        continue
                    if chunk:
                        if binary_sink is not None and descriptor == stdout_descriptor:
                            binary_sink.consume(chunk)
                            if binary_sink.failed and not sink_abort_signaled:
                                sink_abort_signaled = True
                                _signal_process_tree(
                                    process, signal.SIGTERM, owns_process_group,
                                )
                                term_deadline = time.monotonic() + limits.terminate_grace_seconds
                        else:
                            buffer.append(chunk)
                        continue
                    selector.unregister(descriptor)
                    stream.close()
            else:
                time.sleep(_POLL_INTERVAL_SECONDS)

        if process.poll() is None:
            _signal_process_tree(process, signal.SIGKILL, owns_process_group)
        process.wait()
    finally:
        selector.close()
        stdout.close()
        stderr.close()

    return timed_out, lingering_descendants


def _normalize_argv(argv: Sequence[str]) -> tuple[str, ...]:
    if isinstance(argv, str | bytes):
        raise ValueError("argv must be a sequence of argument strings, not a single string")
    normalized = tuple(argv)
    if not normalized:
        raise ValueError("argv must contain at least one element")
    if not isinstance(normalized[0], str) or not normalized[0]:
        raise ValueError("argv[0] must be a non-empty string")
    if any(not isinstance(value, str) for value in normalized[1:]):
        raise ValueError("argv elements after argv[0] must be strings")
    if any("\x00" in value for value in normalized):
        raise ValueError("argv elements must not contain NUL bytes")
    return normalized


def _environment_with_overrides(overrides: Mapping[str, str] | None) -> dict[str, str]:
    environment = dict(os.environ)
    if overrides is None:
        return environment

    for name, value in overrides.items():
        if not isinstance(name, str) or not isinstance(value, str):
            raise ValueError("environment override names and values must be strings")
        if not name or "=" in name or "\x00" in name:
            raise ValueError(
                "environment override names must be non-empty and contain no '=' or NUL"
            )
        if "\x00" in value:
            raise ValueError("environment override values must not contain NUL bytes")
        if name in _LOCAL_AGENT_INTERNAL_ENV_NAMES:
            raise ValueError(f"environment override may not change Local Agent lease state: {name}")
    environment.update(overrides)
    return environment


def _child_environment(environment: Mapping[str, str]) -> dict[str, str]:
    child = dict(environment)
    for name in _LOCAL_AGENT_INTERNAL_ENV_NAMES:
        child.pop(name, None)
    return child


def _validate_local_agent_context(environment: Mapping[str, str]) -> bool:
    repository_raw = environment.get(_LOCAL_AGENT_LEASE_FDS_ENV, "").strip()
    resource_raw = environment.get(_LOCAL_AGENT_RESOURCE_LEASE_FDS_ENV, "").strip()
    digest = environment.get(_LOCAL_AGENT_LEASE_KEYS_DIGEST_ENV, "").strip()

    if not repository_raw:
        if digest:
            raise ValueError("incomplete Local Agent lease state without repository lease")
        if not resource_raw:
            return False
    elif not digest:
        raise ValueError("incomplete Local Agent lease state without repository lease digest")
    if digest and not _LEASE_DIGEST_PATTERN.fullmatch(digest):
        raise ValueError(f"invalid {_LOCAL_AGENT_LEASE_KEYS_DIGEST_ENV}: {digest!r}")

    # Project-free host tasks hold machine/resource leases without inventing
    # a repository identity or digest. Admission remains the outer runner's job.
    repository_fds = (
        _parse_descriptors(_LOCAL_AGENT_LEASE_FDS_ENV, repository_raw)
        if repository_raw else ()
    )
    resource_fds = (
        _parse_descriptors(_LOCAL_AGENT_RESOURCE_LEASE_FDS_ENV, resource_raw)
        if resource_raw
        else ()
    )
    if set(repository_fds) & set(resource_fds):
        raise ValueError("Local Agent repository/resource lease descriptors overlap")

    descriptor_states = tuple(_descriptor_is_open(fd) for fd in (*repository_fds, *resource_fds))
    if any(descriptor_states) and not all(descriptor_states):
        raise ValueError("partially inherited Local Agent lease descriptors")
    return True


def _parse_descriptors(name: str, raw: str) -> tuple[int, ...]:
    parts = raw.split(",")
    try:
        descriptors = tuple(int(item) for item in parts)
    except ValueError:
        raise ValueError(f"invalid {name}: {raw!r}") from None
    if (
        not descriptors
        or any(fd < 3 for fd in descriptors)
        or len(set(descriptors)) != len(descriptors)
    ):
        raise ValueError(f"invalid {name}: {raw!r}")
    return descriptors


def _descriptor_is_open(descriptor: int) -> bool:
    try:
        os.fstat(descriptor)
    except OSError:
        return False
    return True


def _abandon_open_streams(
    selector: selectors.BaseSelector,
    streams: Mapping[int, tuple[BinaryIO, _BoundedBuffer]],
) -> None:
    for descriptor, (_stream, buffer) in streams.items():
        if descriptor not in selector.get_map():
            continue
        buffer.mark_incomplete()
        selector.unregister(descriptor)


def _process_group_alive(process_group_id: int) -> bool:
    try:
        os.killpg(process_group_id, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _signal_process_tree(
    process: subprocess.Popen[bytes],
    sig: signal.Signals,
    owns_process_group: bool,
) -> None:
    if process.poll() is not None:
        return
    if owns_process_group:
        _signal_process_group(process.pid, sig)
        return
    with contextlib.suppress(ProcessLookupError):
        process.send_signal(sig)


def _signal_process_group(process_group_id: int, sig: signal.Signals) -> None:
    with contextlib.suppress(ProcessLookupError):
        os.killpg(process_group_id, sig)
