from __future__ import annotations

import os
import pty
import threading
import time
import tty

import pytest

from local_agent.host_ops.capabilities.local.serial import PosixSerialTransport, SerialTransportError


def test_transact_round_trip_over_pty() -> None:
    master_fd, slave_fd = pty.openpty()
    slave_path = os.ttyname(slave_fd)
    peer_errors: list[BaseException] = []

    def peer() -> None:
        try:
            request = os.read(master_fd, 4)
            assert request == b"PING"
            os.write(master_fd, b"PONG\n")
        except BaseException as exc:  # pragma: no cover - surfaced below
            peer_errors.append(exc)

    thread = threading.Thread(target=peer)
    thread.start()
    try:
        result = PosixSerialTransport().transact(
            slave_path,
            baudrate=115200,
            write_data=b"PING",
            read_limit=64,
            timeout_seconds=1.0,
            idle_seconds=0.05,
        )
    finally:
        thread.join(timeout=1.0)
        os.close(slave_fd)
        os.close(master_fd)

    assert not thread.is_alive()
    assert not peer_errors
    assert result.requested_port == slave_path
    assert result.resolved_port.startswith("/dev/")
    assert result.bytes_written == 4
    assert result.data == b"PONG\n"
    assert result.deadline_reached is False
    assert result.idle_complete is True
    assert result.read_limit_reached is False
    assert result.as_dict()["read_utf8"] == "PONG\n"


def test_transact_settles_before_write_and_flushes_startup_input() -> None:
    master_fd, slave_fd = pty.openpty()
    tty.setraw(slave_fd)
    slave_path = os.ttyname(slave_fd)
    peer_errors: list[BaseException] = []

    def peer() -> None:
        try:
            os.write(master_fd, b"BOOT")
            request = os.read(master_fd, 4)
            assert request == b"PING"
            os.write(master_fd, b"PONG\n")
        except BaseException as exc:  # pragma: no cover - surfaced below
            peer_errors.append(exc)

    thread = threading.Thread(target=peer)
    thread.start()
    started = time.monotonic()
    try:
        result = PosixSerialTransport().transact(
            slave_path,
            baudrate=115200,
            write_data=b"PING",
            read_limit=64,
            timeout_seconds=1.0,
            idle_seconds=0.05,
            settle_seconds=0.05,
        )
    finally:
        thread.join(timeout=1.0)
        os.close(slave_fd)
        os.close(master_fd)

    assert not thread.is_alive()
    assert not peer_errors
    assert result.data == b"PONG\n"
    assert time.monotonic() - started >= 0.05


def test_transact_stops_at_read_limit() -> None:
    master_fd, slave_fd = pty.openpty()
    slave_path = os.ttyname(slave_fd)

    def peer() -> None:
        os.read(master_fd, 1)
        os.write(master_fd, b"abcdefgh")

    thread = threading.Thread(target=peer)
    thread.start()
    try:
        result = PosixSerialTransport().transact(
            slave_path,
            baudrate=9600,
            write_data=b"?",
            read_limit=4,
            timeout_seconds=1.0,
            idle_seconds=0.05,
        )
    finally:
        thread.join(timeout=1.0)
        os.close(slave_fd)
        os.close(master_fd)

    assert not thread.is_alive()
    assert result.data == b"abcd"
    assert result.read_limit_reached is True
    assert result.deadline_reached is False
    assert result.idle_complete is False


def test_transact_reports_deadline_when_no_response_arrives() -> None:
    master_fd, slave_fd = pty.openpty()
    slave_path = os.ttyname(slave_fd)
    try:
        result = PosixSerialTransport().transact(
            slave_path,
            baudrate=9600,
            read_limit=16,
            timeout_seconds=0.05,
            idle_seconds=0.01,
        )
    finally:
        os.close(slave_fd)
        os.close(master_fd)

    assert result.data == b""
    assert result.deadline_reached is True
    assert result.idle_complete is False
    assert result.read_limit_reached is False


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"port": "relative-device", "baudrate": 9600}, "below /dev"),
        ({"port": "/dev/null", "baudrate": -1}, "positive integer"),
        (
            {"port": "/dev/null", "baudrate": 9600, "read_limit": 4 * 1024 * 1024 + 1},
            "read_limit",
        ),
        ({"port": "/dev/null", "baudrate": 9600, "timeout_seconds": 0.0}, "timeout_seconds"),
        (
            {"port": "/dev/null", "baudrate": 9600, "timeout_seconds": 1.0, "idle_seconds": 2.0},
            "idle_seconds",
        ),
        ({"port": "/dev/null", "baudrate": 9600, "settle_seconds": -0.1}, "settle_seconds"),
        (
            {"port": "/dev/null", "baudrate": 9600, "timeout_seconds": 1.0, "settle_seconds": 1.0},
            "settle_seconds",
        ),
    ],
)
def test_transact_rejects_invalid_inputs(kwargs: dict[str, object], message: str) -> None:
    with pytest.raises(SerialTransportError, match=message):
        PosixSerialTransport().transact(**kwargs)  # type: ignore[arg-type]
