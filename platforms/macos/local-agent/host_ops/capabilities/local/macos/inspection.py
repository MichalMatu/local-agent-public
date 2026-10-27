"""Read-only inspection of macOS host, USB, serial and external storage state."""

from __future__ import annotations

import platform
import plistlib
import re
import time
from collections.abc import Iterator, Mapping

from local_agent.host_ops.core.execution import ExecutionLimits, ProcessResult, ProcessRunner

from .models import MacOSHostInfo, MacOSSerialPort, MacOSStorageDevice, MacOSUSBDevice

_SW_VERS = "/usr/bin/sw_vers"
_UNAME = "/usr/bin/uname"
_IOREG = "/usr/sbin/ioreg"
_DISKUTIL = "/usr/sbin/diskutil"
_DISK_IDENTIFIER = re.compile(r"^disk\d+(?:s\d+)?$")
_HEX_LOCATION = re.compile(r"^[0-9a-fA-F]{8}$")
_DEFAULT_LIMITS = ExecutionLimits()


class MacOSInspectionError(RuntimeError):
    """Raised when macOS inspection cannot produce trustworthy structured evidence."""


class _OperationBudget:
    def __init__(self, limits: ExecutionLimits) -> None:
        self._limits = limits
        self._deadline = time.monotonic() + limits.timeout_seconds

    def remaining(self) -> ExecutionLimits:
        remaining = self._deadline - time.monotonic()
        if remaining <= 0:
            raise MacOSInspectionError("macOS inspection exceeded its whole-operation timeout")
        return ExecutionLimits(
            timeout_seconds=remaining,
            terminate_grace_seconds=self._limits.terminate_grace_seconds,
            pipe_drain_seconds=self._limits.pipe_drain_seconds,
            max_stdout_bytes=self._limits.max_stdout_bytes,
            max_stderr_bytes=self._limits.max_stderr_bytes,
        )


class MacOSInspector:
    """Inspect local macOS devices through pinned native tools and bounded execution."""

    def __init__(
        self,
        runner: ProcessRunner | None = None,
        *,
        system_name: str | None = None,
    ) -> None:
        self._runner = runner or ProcessRunner()
        self._system_name = system_name or platform.system()

    def host_info(self, *, limits: ExecutionLimits | None = None) -> MacOSHostInfo:
        self._require_macos()
        budget = _OperationBudget(limits or _DEFAULT_LIMITS)
        version = _single_line(
            self._run((_SW_VERS, "-productVersion"), limits=budget.remaining()),
            "read macOS product version",
        )
        architecture = _single_line(
            self._run((_UNAME, "-m"), limits=budget.remaining()),
            "read host architecture",
        )
        return MacOSHostInfo(product_version=version, architecture=architecture)

    def usb_devices(self, *, limits: ExecutionLimits | None = None) -> tuple[MacOSUSBDevice, ...]:
        self._require_macos()
        payload = _plist(
            self._run((_IOREG, "-r", "-c", "IOUSBHostDevice", "-a"), limits=limits),
            "inspect USB devices",
        )
        if not isinstance(payload, list):
            raise MacOSInspectionError("inspect USB devices: unexpected ioreg schema")

        devices: list[MacOSUSBDevice] = []
        seen: set[tuple[str | None, str, str]] = set()
        for item in _iter_ioreg_entries(payload):
            vendor_id = _usb_id(item.get("idVendor"))
            product_id = _usb_id(item.get("idProduct"))
            if vendor_id is None or product_id is None:
                continue
            name = _first_string(
                item,
                "USB Product Name",
                "kUSBProductString",
                "IORegistryEntryName",
            )
            if name is None:
                continue
            serial_number = _first_string(item, "USB Serial Number", "kUSBSerialNumberString")
            location_id = _usb_location(item)
            identity = (location_id, vendor_id, product_id)
            if identity in seen:
                continue
            seen.add(identity)
            devices.append(
                MacOSUSBDevice(
                    name=name,
                    vendor_id=vendor_id,
                    product_id=product_id,
                    serial_number=serial_number,
                    location_id=location_id,
                )
            )
        return tuple(devices)

    def serial_ports(self, *, limits: ExecutionLimits | None = None) -> tuple[MacOSSerialPort, ...]:
        self._require_macos()
        result = self._run((_IOREG, "-r", "-c", "IOSerialBSDClient", "-a"), limits=limits)
        payload = _plist(result, "inspect serial ports")
        if not isinstance(payload, list):
            raise MacOSInspectionError("inspect serial ports: unexpected ioreg schema")

        ports: list[MacOSSerialPort] = []
        seen: set[str] = set()
        for raw in payload:
            if not isinstance(raw, dict):
                continue
            callout = _optional_string(raw.get("IOCalloutDevice"))
            if callout is None or callout in seen:
                continue
            seen.add(callout)
            ports.append(
                MacOSSerialPort(
                    callout_device=callout,
                    dialin_device=_optional_string(raw.get("IODialinDevice")),
                    tty_device=_optional_string(raw.get("IOTTYDevice")),
                )
            )
        return tuple(ports)

    def external_storage(
        self,
        *,
        limits: ExecutionLimits | None = None,
    ) -> tuple[MacOSStorageDevice, ...]:
        self._require_macos()
        budget = _OperationBudget(limits or _DEFAULT_LIMITS)
        listing = _plist(
            self._run(
                (_DISKUTIL, "list", "-plist", "external", "physical"),
                limits=budget.remaining(),
            ),
            "list external physical storage",
        )
        if not isinstance(listing, dict):
            raise MacOSInspectionError("list external physical storage: unexpected diskutil schema")
        raw_identifiers = listing.get("AllDisks", [])
        if not isinstance(raw_identifiers, list):
            raise MacOSInspectionError("list external physical storage: missing AllDisks list")

        devices: list[MacOSStorageDevice] = []
        for raw_identifier in raw_identifiers:
            identifier = _optional_string(raw_identifier)
            if identifier is None or not _DISK_IDENTIFIER.fullmatch(identifier):
                continue
            info = _plist(
                self._run(
                    (_DISKUTIL, "info", "-plist", identifier),
                    limits=budget.remaining(),
                ),
                f"inspect storage device {identifier}",
            )
            if not isinstance(info, dict):
                raise MacOSInspectionError(
                    f"inspect storage device {identifier}: unexpected diskutil schema"
                )
            devices.append(_storage_device(identifier, info))
        return tuple(devices)

    def _require_macos(self) -> None:
        if self._system_name != "Darwin":
            raise MacOSInspectionError(
                f"macOS capability requires Darwin, current system is {self._system_name!r}"
            )

    def _run(
        self,
        argv: tuple[str, ...],
        *,
        limits: ExecutionLimits | None,
    ) -> ProcessResult:
        return self._runner.run(argv, limits=limits)


def _iter_ioreg_entries(value: object) -> Iterator[Mapping[str, object]]:
    if isinstance(value, list):
        for item in value:
            yield from _iter_ioreg_entries(item)
        return
    if not isinstance(value, dict):
        return
    yield value
    children = value.get("IORegistryEntryChildren")
    if children is not None:
        yield from _iter_ioreg_entries(children)


def _storage_device(identifier: str, info: Mapping[object, object]) -> MacOSStorageDevice:
    return MacOSStorageDevice(
        identifier=identifier,
        device_node=_optional_string(info.get("DeviceNode")),
        whole=_optional_bool(info.get("Whole")) or False,
        part_of_whole=_optional_string(info.get("PartOfWhole")),
        media_name=_optional_string(info.get("MediaName")),
        volume_name=_optional_string(info.get("VolumeName")),
        mount_point=_optional_string(info.get("MountPoint")),
        filesystem=_optional_string(info.get("FileSystemPersonality")),
        protocol=_optional_string(info.get("Protocol")),
        size_bytes=_optional_int(info.get("DiskSize")),
        internal=_optional_bool(info.get("Internal")),
        ejectable=_optional_bool(info.get("Ejectable")),
        removable_media=_optional_bool(info.get("RemovableMedia")),
        read_only_media=_optional_bool(info.get("ReadOnlyMedia")),
        read_only_volume=_optional_bool(info.get("ReadOnlyVolume")),
    )


def _single_line(result: ProcessResult, action: str) -> str:
    _require_ok(result, action)
    value = result.stdout.strip()
    if not value or "\n" in value:
        raise MacOSInspectionError(f"{action}: unexpected command output")
    return value


def _plist(result: ProcessResult, action: str) -> object:
    _require_ok(result, action)
    try:
        return plistlib.loads(result.stdout.encode("utf-8"))
    except (plistlib.InvalidFileException, ValueError) as exc:
        raise MacOSInspectionError(f"{action}: invalid plist output") from exc


def _require_ok(result: ProcessResult, action: str) -> None:
    if result.ok and not result.stdout_truncated:
        return
    if result.stdout_truncated:
        raise MacOSInspectionError(f"{action}: command stdout was truncated")
    detail = result.stderr.strip() or result.error or f"exit_code={result.exit_code}"
    raise MacOSInspectionError(f"{action}: {detail}")


def _first_string(value: Mapping[str, object], *keys: str) -> str | None:
    for key in keys:
        text = _optional_string(value.get(key))
        if text is not None:
            return text
    return None


def _optional_string(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _optional_bool(value: object) -> bool | None:
    return value if isinstance(value, bool) else None


def _optional_int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _usb_id(value: object) -> str | None:
    number = _optional_int(value)
    if number is None or not 0 <= number <= 0xFFFF:
        return None
    return f"0x{number:04x}"


def _usb_location(value: Mapping[str, object]) -> str | None:
    numeric = _optional_int(value.get("locationID"))
    if numeric is not None and 0 <= numeric <= 0xFFFFFFFF:
        return f"0x{numeric:08x}"
    registry = _optional_string(value.get("IORegistryEntryLocation"))
    if registry is None or _HEX_LOCATION.fullmatch(registry) is None:
        return None
    return f"0x{registry.lower()}"
