"""CLI surface for bounded ADB inspection and verified file transfer."""

from __future__ import annotations

import json
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path

from local_agent.host_ops.capabilities.local.adb import (
    AdbClient,
    AdbFileTransfer,
    AdbInspectionError,
    AdbTransferError,
)
from local_agent.host_ops.core.execution import ExecutionLimits


def run_devices(*, timeout_seconds: float, as_json: bool) -> int:
    try:
        devices = AdbClient().devices(limits=_inspection_limits(timeout_seconds))
    except (AdbInspectionError, ValueError) as exc:
        return _inspection_error(str(exc), as_json=as_json)
    rows = [device.as_dict() for device in devices]
    if as_json:
        print(json.dumps(rows, sort_keys=True))
        return 0
    if not rows:
        print("No ADB devices found")
        return 0
    _render_rows(rows)
    return 0


def run_identity(serial: str, *, timeout_seconds: float, as_json: bool) -> int:
    try:
        identity = AdbClient().identity(serial, limits=_inspection_limits(timeout_seconds))
    except (AdbInspectionError, ValueError) as exc:
        return _inspection_error(str(exc), as_json=as_json)
    payload = identity.as_dict()
    if as_json:
        print(json.dumps(payload, sort_keys=True))
    else:
        _render_rows((payload,))
    return 0


def run_logcat(
    serial: str,
    *,
    lines: int,
    timeout_seconds: float,
    as_json: bool,
) -> int:
    try:
        result = AdbClient().logcat(
            serial,
            lines=lines,
            limits=_inspection_limits(timeout_seconds),
        )
    except (AdbInspectionError, ValueError) as exc:
        return _inspection_error(str(exc), as_json=as_json)
    if as_json:
        print(json.dumps(result.as_dict(), sort_keys=True))
    else:
        print(result.text, end="" if result.text.endswith("\n") or not result.text else "\n")
    return 0


def run_push(
    serial: str,
    local_source: str,
    remote_destination: str,
    *,
    replace: bool,
    max_bytes: int,
    timeout_seconds: float,
    as_json: bool,
) -> int:
    try:
        result = AdbFileTransfer().push(
            serial,
            Path(local_source),
            remote_destination,
            replace=replace,
            max_bytes=max_bytes,
            limits=_transfer_limits(timeout_seconds),
        )
    except (AdbTransferError, ValueError) as exc:
        return _transfer_error(exc, as_json=as_json)
    return _render_transfer(result.as_dict(), as_json=as_json)


def run_pull(
    serial: str,
    remote_source: str,
    local_destination: str,
    *,
    replace: bool,
    max_bytes: int,
    timeout_seconds: float,
    as_json: bool,
) -> int:
    try:
        result = AdbFileTransfer().pull(
            serial,
            remote_source,
            Path(local_destination),
            replace=replace,
            max_bytes=max_bytes,
            limits=_transfer_limits(timeout_seconds),
        )
    except (AdbTransferError, ValueError) as exc:
        return _transfer_error(exc, as_json=as_json)
    return _render_transfer(result.as_dict(), as_json=as_json)


def _inspection_limits(timeout_seconds: float) -> ExecutionLimits:
    return ExecutionLimits(
        timeout_seconds=timeout_seconds,
        max_stdout_bytes=128 * 1024,
        max_stderr_bytes=32 * 1024,
    )


def _transfer_limits(timeout_seconds: float) -> ExecutionLimits:
    return ExecutionLimits(
        timeout_seconds=timeout_seconds,
        max_stdout_bytes=128 * 1024,
        max_stderr_bytes=64 * 1024,
    )


def _render_rows(rows: Sequence[Mapping[str, object]]) -> None:
    for row in rows:
        print(" ".join(f"{key}={value}" for key, value in row.items() if value is not None))


def _render_transfer(payload: Mapping[str, object], *, as_json: bool) -> int:
    if as_json:
        print(json.dumps(dict(payload), sort_keys=True))
    else:
        print(
            f"ADB {payload['direction']} {payload['source']} -> {payload['destination']} "
            f"bytes={payload['size_bytes']} sha256={payload['sha256']}"
        )
    return 0


def _inspection_error(message: str, *, as_json: bool) -> int:
    if as_json:
        print(json.dumps({"error": message}, sort_keys=True), file=sys.stderr)
    else:
        print(f"ADB inspection failed: {message}", file=sys.stderr)
    return 1


def _transfer_error(error: AdbTransferError | ValueError, *, as_json: bool) -> int:
    message = str(error)
    if as_json:
        payload: dict[str, object] = {"error": message}
        if isinstance(error, AdbTransferError):
            payload.update(
                action_attempted=error.action_attempted,
                committed=error.committed,
                cleanup_failed=error.cleanup_failed,
            )
        print(json.dumps(payload, sort_keys=True), file=sys.stderr)
    else:
        print(f"ADB transfer failed: {message}", file=sys.stderr)
    return 1
