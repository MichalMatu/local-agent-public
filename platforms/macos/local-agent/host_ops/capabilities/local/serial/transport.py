"""Bounded POSIX serial transactions for explicitly identified local device ports."""

from __future__ import annotations

import math
import os
import select
import stat
import termios
import time
from pathlib import Path

from .models import SerialTransactionResult

_MAX_WRITE_BYTES = 1024 * 1024
_MAX_READ_BYTES = 4 * 1024 * 1024
_MAX_TIMEOUT_SECONDS = 60.0


class SerialTransportError(RuntimeError):
    """Raised when a bounded serial transaction cannot complete safely."""


class PosixSerialTransport:
    """Perform one bounded 8N1 serial write/read transaction on a POSIX TTY."""

    def transact(
        self,
        port: str,
        *,
        baudrate: int,
        write_data: bytes = b"",
        read_limit: int = 4096,
        timeout_seconds: float = 1.0,
        idle_seconds: float = 0.1,
        settle_seconds: float = 0.0,
    ) -> SerialTransactionResult:
        speed = _baud_constant(baudrate)
        _validate_bounds(
            write_data=write_data,
            read_limit=read_limit,
            timeout_seconds=timeout_seconds,
            idle_seconds=idle_seconds,
            settle_seconds=settle_seconds,
        )
        requested_port, resolved_port = _resolve_port(port)

        started = time.monotonic()
        deadline = started + timeout_seconds
        descriptor: int | None = None
        try:
            descriptor = os.open(
                resolved_port,
                os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK,
            )
            _configure_serial(descriptor, speed)
            _settle_before_io(settle_seconds=settle_seconds, deadline=deadline)
            if write_data:
                termios.tcflush(descriptor, termios.TCIFLUSH)
            bytes_written = _write_all(descriptor, write_data, deadline)
            data, deadline_reached, idle_complete, read_limit_reached = _read_bounded(
                descriptor,
                read_limit=read_limit,
                deadline=deadline,
                idle_seconds=idle_seconds,
            )
        except SerialTransportError:
            raise
        except (OSError, termios.error) as exc:
            raise SerialTransportError(
                f"serial transaction failed for {resolved_port}: {exc}"
            ) from exc
        finally:
            if descriptor is not None:
                os.close(descriptor)

        return SerialTransactionResult(
            requested_port=requested_port,
            resolved_port=resolved_port,
            baudrate=baudrate,
            bytes_written=bytes_written,
            data=data,
            elapsed_seconds=time.monotonic() - started,
            deadline_reached=deadline_reached,
            idle_complete=idle_complete,
            read_limit_reached=read_limit_reached,
        )


def _resolve_port(value: str) -> tuple[str, str]:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise SerialTransportError("serial port must be a non-empty device path")
    requested = Path(value).expanduser()
    if not requested.is_absolute() or requested.parts[:2] != ("/", "dev"):
        raise SerialTransportError("serial port must be an absolute path below /dev")
    try:
        resolved = requested.resolve(strict=True)
        mode = resolved.stat().st_mode
    except OSError as exc:
        raise SerialTransportError(f"serial port cannot be resolved: {requested}") from exc
    if resolved.parts[:2] != ("/", "dev"):
        raise SerialTransportError("serial port must resolve below /dev")
    if not stat.S_ISCHR(mode):
        raise SerialTransportError(f"serial port is not a character device: {resolved}")
    return str(requested), str(resolved)


def _baud_constant(baudrate: int) -> int:
    if isinstance(baudrate, bool) or not isinstance(baudrate, int) or baudrate <= 0:
        raise SerialTransportError("baudrate must be a positive integer")
    value = getattr(termios, f"B{baudrate}", None)
    if not isinstance(value, int):
        raise SerialTransportError(f"baudrate is not supported on this host: {baudrate}")
    return value


def _validate_bounds(
    *,
    write_data: bytes,
    read_limit: int,
    timeout_seconds: float,
    idle_seconds: float,
    settle_seconds: float,
) -> None:
    if not isinstance(write_data, bytes):
        raise SerialTransportError("write_data must be bytes")
    if len(write_data) > _MAX_WRITE_BYTES:
        raise SerialTransportError(f"write payload exceeds {_MAX_WRITE_BYTES} bytes")
    if (
        isinstance(read_limit, bool)
        or not isinstance(read_limit, int)
        or not 0 <= read_limit <= _MAX_READ_BYTES
    ):
        raise SerialTransportError(f"read_limit must be between 0 and {_MAX_READ_BYTES}")
    if (
        isinstance(timeout_seconds, bool)
        or not isinstance(timeout_seconds, int | float)
        or not math.isfinite(timeout_seconds)
        or not 0 < timeout_seconds <= _MAX_TIMEOUT_SECONDS
    ):
        raise SerialTransportError(
            f"timeout_seconds must be greater than 0 and at most {_MAX_TIMEOUT_SECONDS}"
        )
    if (
        isinstance(idle_seconds, bool)
        or not isinstance(idle_seconds, int | float)
        or not math.isfinite(idle_seconds)
        or not 0 < idle_seconds <= timeout_seconds
    ):
        raise SerialTransportError(
            "idle_seconds must be greater than 0 and at most timeout_seconds"
        )
    if (
        isinstance(settle_seconds, bool)
        or not isinstance(settle_seconds, int | float)
        or not math.isfinite(settle_seconds)
        or not 0 <= settle_seconds < timeout_seconds
    ):
        raise SerialTransportError(
            "settle_seconds must be at least 0 and less than timeout_seconds"
        )


def _configure_serial(descriptor: int, speed: int) -> None:
    attributes = termios.tcgetattr(descriptor)
    attributes[0] = 0
    attributes[1] = 0
    control = attributes[2]
    control &= ~(termios.PARENB | termios.CSTOPB | termios.CSIZE)
    if hasattr(termios, "CRTSCTS"):
        control &= ~termios.CRTSCTS
    control |= termios.CLOCAL | termios.CREAD | termios.CS8
    attributes[2] = control
    attributes[3] = 0
    attributes[4] = speed
    attributes[5] = speed
    attributes[6][termios.VMIN] = 0
    attributes[6][termios.VTIME] = 0
    termios.tcsetattr(descriptor, termios.TCSANOW, attributes)


def _settle_before_io(*, settle_seconds: float, deadline: float) -> None:
    if settle_seconds == 0:
        return
    if deadline - time.monotonic() < settle_seconds:
        raise SerialTransportError("serial settle delay would exceed transaction deadline")
    time.sleep(settle_seconds)


def _write_all(descriptor: int, payload: bytes, deadline: float) -> int:
    offset = 0
    while offset < len(payload):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise SerialTransportError(
                f"serial write deadline reached after {offset} of {len(payload)} bytes"
            )
        _, writable, _ = select.select([], [descriptor], [], remaining)
        if not writable:
            raise SerialTransportError(
                f"serial write deadline reached after {offset} of {len(payload)} bytes"
            )
        try:
            written = os.write(descriptor, payload[offset:])
        except BlockingIOError:
            continue
        if written <= 0:
            raise SerialTransportError("serial write made no forward progress")
        offset += written
    return offset


def _read_bounded(
    descriptor: int,
    *,
    read_limit: int,
    deadline: float,
    idle_seconds: float,
) -> tuple[bytes, bool, bool, bool]:
    if read_limit == 0:
        return b"", False, False, False

    data = bytearray()
    last_read_at: float | None = None
    while len(data) < read_limit:
        now = time.monotonic()
        deadline_remaining = deadline - now
        if deadline_remaining <= 0:
            return bytes(data), True, False, False

        wait_seconds = deadline_remaining
        if last_read_at is not None:
            idle_remaining = idle_seconds - (now - last_read_at)
            if idle_remaining <= 0:
                return bytes(data), False, True, False
            wait_seconds = min(wait_seconds, idle_remaining)

        readable, _, _ = select.select([descriptor], [], [], wait_seconds)
        if not readable:
            now = time.monotonic()
            if last_read_at is not None and now - last_read_at >= idle_seconds:
                return bytes(data), False, True, False
            return bytes(data), True, False, False

        try:
            chunk = os.read(descriptor, min(65536, read_limit - len(data)))
        except BlockingIOError:
            continue
        if not chunk:
            raise SerialTransportError("serial device closed while reading")
        data.extend(chunk)
        last_read_at = time.monotonic()

    return bytes(data), False, False, True
