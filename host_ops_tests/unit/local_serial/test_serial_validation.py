from __future__ import annotations

import importlib

import pytest

serial = importlib.import_module("local_agent.host_ops.capabilities.local.serial.transport")


def test_serial_path_rejects_nonstrings_with_controlled_error() -> None:
    with pytest.raises(serial.SerialTransportError, match="device path"):
        serial._resolve_port(123)


@pytest.mark.parametrize("value", [True, "115200", 0, -1])
def test_baudrate_rejects_boolean_and_noninteger_values(value) -> None:
    with pytest.raises(serial.SerialTransportError, match="positive integer"):
        serial._baud_constant(value)


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"write_data": "bytes-only"}, "write_data must be bytes"),
        ({"read_limit": True}, "read_limit"),
        ({"read_limit": "10"}, "read_limit"),
        ({"timeout_seconds": True}, "timeout_seconds"),
        ({"timeout_seconds": float("nan")}, "timeout_seconds"),
        ({"timeout_seconds": float("inf")}, "timeout_seconds"),
        ({"idle_seconds": True}, "idle_seconds"),
        ({"idle_seconds": float("nan")}, "idle_seconds"),
        ({"settle_seconds": True}, "settle_seconds"),
        ({"settle_seconds": float("nan")}, "settle_seconds"),
    ],
)
def test_serial_bounds_reject_invalid_direct_api_values(
    overrides: dict[str, object], message: str
) -> None:
    values: dict[str, object] = {
        "write_data": b"data",
        "read_limit": 4096,
        "timeout_seconds": 1.0,
        "idle_seconds": 0.1,
        "settle_seconds": 0.0,
    }
    values.update(overrides)
    with pytest.raises(serial.SerialTransportError, match=message):
        serial._validate_bounds(**values)


def test_public_serial_rejects_invalid_payload_before_device_lookup() -> None:
    with pytest.raises(serial.SerialTransportError, match="write_data must be bytes"):
        serial.PosixSerialTransport().transact(
            "/dev/definitely-missing-hostops-test-device",
            baudrate=115200,
            write_data="not-bytes",
        )
