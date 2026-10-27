"""CLI rendering for bounded local serial transactions."""

from __future__ import annotations

import json
import sys

from local_agent.host_ops.capabilities.local.serial import PosixSerialTransport, SerialTransportError


def run_transact(
    port: str,
    *,
    baudrate: int,
    write_text: str | None,
    write_hex: str | None,
    read_limit: int,
    timeout_seconds: float,
    idle_seconds: float,
    settle_seconds: float,
    as_json: bool,
) -> int:
    try:
        payload = _payload(write_text=write_text, write_hex=write_hex)
        result = PosixSerialTransport().transact(
            port,
            baudrate=baudrate,
            write_data=payload,
            read_limit=read_limit,
            timeout_seconds=timeout_seconds,
            idle_seconds=idle_seconds,
            settle_seconds=settle_seconds,
        )
    except (SerialTransportError, ValueError) as exc:
        return _error(str(exc), as_json=as_json)

    output = result.as_dict()
    if as_json:
        print(json.dumps(output, sort_keys=True))
    else:
        print(
            f"port={result.resolved_port} baudrate={result.baudrate} "
            f"written={result.bytes_written} read={len(result.data)} "
            f"deadline_reached={result.deadline_reached} "
            f"idle_complete={result.idle_complete} "
            f"read_limit_reached={result.read_limit_reached}"
        )
        if result.data:
            try:
                print(result.data.decode("utf-8"), end="" if result.data.endswith(b"\n") else "\n")
            except UnicodeDecodeError:
                print(f"hex={result.data.hex()}")
    return 0


def _payload(*, write_text: str | None, write_hex: str | None) -> bytes:
    if write_text is not None:
        return write_text.encode("utf-8")
    if write_hex is None:
        return b""
    try:
        return bytes.fromhex(write_hex)
    except ValueError as exc:
        raise ValueError("write_hex must contain valid hexadecimal bytes") from exc


def _error(message: str, *, as_json: bool) -> int:
    if as_json:
        print(json.dumps({"error": message}, sort_keys=True), file=sys.stderr)
    else:
        print(f"serial transaction failed: {message}", file=sys.stderr)
    return 1
