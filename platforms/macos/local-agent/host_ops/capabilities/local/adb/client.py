"""Bounded read-only Android Debug Bridge inspection."""

from __future__ import annotations

import os
import re
import shutil
import time
from collections.abc import Callable
from pathlib import Path

from local_agent.host_ops.core.execution import ExecutionLimits, ProcessResult, ProcessRunner

from .models import AdbDevice, AdbIdentity, AdbLogcatResult

_SERIAL_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,254}$")
_PROPERTY_PATTERN = re.compile(r"^\[([^]]+)\]: \[(.*)\]$")
_DEFAULT_LIMITS = ExecutionLimits(
    timeout_seconds=10.0,
    max_stdout_bytes=128 * 1024,
    max_stderr_bytes=32 * 1024,
)
_MAX_LOGCAT_LINES = 10_000
_IDENTITY_PROPERTIES = (
    "ro.product.manufacturer",
    "ro.product.model",
    "ro.product.name",
    "ro.product.device",
    "ro.build.version.release",
    "ro.build.version.sdk",
    "ro.build.fingerprint",
)


class AdbInspectionError(RuntimeError):
    """Raised when ADB inspection cannot produce trustworthy evidence."""


class AdbClient:
    """Inspect explicitly selected Android devices through a bounded ADB process."""

    def __init__(
        self,
        runner: ProcessRunner | None = None,
        *,
        resolver: Callable[[str], str | None] = shutil.which,
    ) -> None:
        self._runner = runner or ProcessRunner()
        self._resolver = resolver

    def devices(self, *, limits: ExecutionLimits | None = None) -> tuple[AdbDevice, ...]:
        executable = self._resolve_executable()
        result = self._runner.run(
            (executable, "devices", "-l"),
            limits=limits or _DEFAULT_LIMITS,
        )
        _require_ok(result, "list ADB devices")
        return _parse_devices(result.stdout)

    def identity(
        self,
        serial: str,
        *,
        limits: ExecutionLimits | None = None,
    ) -> AdbIdentity:
        normalized_serial = _normalize_serial(serial)
        executable = self._resolve_executable()
        budget = _OperationBudget(limits or _DEFAULT_LIMITS)

        listing = self._runner.run(
            (executable, "devices", "-l"),
            limits=budget.remaining(),
        )
        _require_ok(listing, "list ADB devices")
        devices = _parse_devices(listing.stdout)
        device = next((item for item in devices if item.serial == normalized_serial), None)
        if device is None:
            raise AdbInspectionError(f"ADB device not found: {normalized_serial}")
        if device.state != "device":
            raise AdbInspectionError(
                f"ADB device {normalized_serial} is not ready; state={device.state!r}"
            )

        properties = {
            name: _read_property(
                self._runner,
                executable,
                normalized_serial,
                name,
                budget,
            )
            for name in _IDENTITY_PROPERTIES
        }
        return AdbIdentity(
            serial=normalized_serial,
            state=device.state,
            manufacturer=properties.get("ro.product.manufacturer"),
            model=properties.get("ro.product.model"),
            product=properties.get("ro.product.name"),
            device=properties.get("ro.product.device"),
            android_version=properties.get("ro.build.version.release"),
            sdk_level=properties.get("ro.build.version.sdk"),
            build_fingerprint=properties.get("ro.build.fingerprint"),
        )

    def logcat(
        self,
        serial: str,
        *,
        lines: int = 200,
        limits: ExecutionLimits | None = None,
    ) -> AdbLogcatResult:
        normalized_serial = _normalize_serial(serial)
        line_limit = _normalize_logcat_lines(lines)
        executable = self._resolve_executable()
        budget = _OperationBudget(limits or _DEFAULT_LIMITS)
        _require_ready_device(
            self._runner,
            executable,
            normalized_serial,
            budget,
        )
        result = self._runner.run(
            (
                executable,
                "-s",
                normalized_serial,
                "logcat",
                "-d",
                "-t",
                str(line_limit),
            ),
            limits=budget.remaining(),
        )
        _require_ok(result, f"capture ADB logcat for {normalized_serial}")
        text = result.stdout
        return AdbLogcatResult(
            serial=normalized_serial,
            lines_requested=line_limit,
            line_count=len(text.splitlines()),
            text=text,
        )

    def _resolve_executable(self) -> str:
        resolved = self._resolver("adb")
        if resolved is None:
            raise AdbInspectionError("adb executable was not found on PATH")
        path = Path(os.path.realpath(os.path.expanduser(resolved)))
        if not path.is_absolute():
            raise AdbInspectionError("resolved adb executable path is not absolute")
        return str(path)


class _OperationBudget:
    def __init__(self, limits: ExecutionLimits) -> None:
        self._limits = limits
        self._deadline = time.monotonic() + limits.timeout_seconds

    def remaining(self) -> ExecutionLimits:
        remaining = self._deadline - time.monotonic()
        if remaining <= 0:
            raise AdbInspectionError("ADB inspection exceeded its whole-operation timeout")
        return ExecutionLimits(
            timeout_seconds=remaining,
            terminate_grace_seconds=self._limits.terminate_grace_seconds,
            pipe_drain_seconds=self._limits.pipe_drain_seconds,
            max_stdout_bytes=self._limits.max_stdout_bytes,
            max_stderr_bytes=self._limits.max_stderr_bytes,
        )


def _require_ready_device(
    runner: ProcessRunner,
    executable: str,
    serial: str,
    budget: _OperationBudget,
) -> None:
    result = runner.run(
        (executable, "-s", serial, "get-state"),
        limits=budget.remaining(),
    )
    _require_ok(result, f"check ADB device {serial}")
    if result.stdout.strip() != "device":
        raise AdbInspectionError(f"ADB device {serial} is not ready")


def _parse_devices(stdout: str) -> tuple[AdbDevice, ...]:
    lines = stdout.splitlines()
    while lines and not lines[0].strip():
        lines.pop(0)
    if not lines or lines[0].strip() != "List of devices attached":
        raise AdbInspectionError("adb devices returned an unexpected header")

    devices: list[AdbDevice] = []
    seen: set[str] = set()
    for raw_line in lines[1:]:
        line = raw_line.strip()
        if not line:
            continue
        fields = line.split()
        if len(fields) < 2:
            raise AdbInspectionError("adb devices returned a malformed device row")
        serial = _normalize_serial(fields[0])
        if serial in seen:
            raise AdbInspectionError(f"adb devices returned duplicate serial: {serial}")
        seen.add(serial)

        state = "no permissions" if fields[1:3] == ["no", "permissions"] else fields[1]
        metadata_start = 3 if state == "no permissions" else 2
        metadata = _metadata(fields[metadata_start:])
        devices.append(
            AdbDevice(
                serial=serial,
                state=state,
                product=metadata.get("product"),
                model=metadata.get("model"),
                device=metadata.get("device"),
                transport_id=metadata.get("transport_id"),
                usb=metadata.get("usb"),
            )
        )
    return tuple(devices)


def _metadata(fields: list[str]) -> dict[str, str]:
    values: dict[str, str] = {}
    for field in fields:
        key, separator, value = field.partition(":")
        if separator and key and value:
            values[key] = value
    return values


def _read_property(
    runner: ProcessRunner,
    executable: str,
    serial: str,
    name: str,
    budget: _OperationBudget,
) -> str | None:
    result = runner.run(
        (executable, "-s", serial, "shell", "getprop", name),
        limits=budget.remaining(),
    )
    _require_ok(result, f"read ADB property {name!r} for {serial}")
    value = result.stdout.rstrip("\r\n")
    if "\n" in value or "\r" in value:
        raise AdbInspectionError(f"adb getprop {name!r} returned multiline output")
    return value or None


def _normalize_serial(serial: str) -> str:
    if not isinstance(serial, str) or _SERIAL_PATTERN.fullmatch(serial) is None:
        raise AdbInspectionError("ADB serial contains unsupported characters")
    return serial


def _normalize_logcat_lines(lines: int) -> int:
    if isinstance(lines, bool) or not isinstance(lines, int) or not 1 <= lines <= _MAX_LOGCAT_LINES:
        raise AdbInspectionError(f"logcat lines must be an integer in range 1..{_MAX_LOGCAT_LINES}")
    return lines


def _require_ok(result: ProcessResult, action: str) -> None:
    if result.ok and not result.stdout_truncated and not result.stderr_truncated:
        return
    if result.stdout_truncated or result.stderr_truncated:
        raise AdbInspectionError(f"{action}: command output was truncated")
    detail = result.stderr.strip() or result.error or f"exit_code={result.exit_code}"
    raise AdbInspectionError(f"{action}: {detail}")
