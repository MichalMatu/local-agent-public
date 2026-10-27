"""Structured Android Debug Bridge operation results."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class AdbDevice:
    serial: str
    state: str
    product: str | None = None
    model: str | None = None
    device: str | None = None
    transport_id: str | None = None
    usb: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "serial": self.serial,
            "state": self.state,
            "product": self.product,
            "model": self.model,
            "device": self.device,
            "transport_id": self.transport_id,
            "usb": self.usb,
        }


@dataclass(frozen=True, slots=True)
class AdbIdentity:
    serial: str
    state: str
    manufacturer: str | None = None
    model: str | None = None
    product: str | None = None
    device: str | None = None
    android_version: str | None = None
    sdk_level: str | None = None
    build_fingerprint: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "serial": self.serial,
            "state": self.state,
            "manufacturer": self.manufacturer,
            "model": self.model,
            "product": self.product,
            "device": self.device,
            "android_version": self.android_version,
            "sdk_level": self.sdk_level,
            "build_fingerprint": self.build_fingerprint,
        }


@dataclass(frozen=True, slots=True)
class AdbLogcatResult:
    serial: str
    lines_requested: int
    line_count: int
    text: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "serial": self.serial,
            "lines_requested": self.lines_requested,
            "line_count": self.line_count,
            "text": self.text,
        }


@dataclass(frozen=True, slots=True)
class AdbTransferResult:
    direction: str
    serial: str
    source: str
    destination: str
    size_bytes: int
    sha256: str
    replaced_existing: bool
    local_directory_synced: bool | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "direction": self.direction,
            "serial": self.serial,
            "source": self.source,
            "destination": self.destination,
            "size_bytes": self.size_bytes,
            "sha256": self.sha256,
            "replaced_existing": self.replaced_existing,
            "local_directory_synced": self.local_directory_synced,
        }
