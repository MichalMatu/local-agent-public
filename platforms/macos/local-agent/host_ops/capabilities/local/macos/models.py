"""Structured results for macOS host, device and storage operations."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class MacOSHostInfo:
    product_version: str
    architecture: str

    def as_dict(self) -> dict[str, object]:
        return {
            "product_version": self.product_version,
            "architecture": self.architecture,
        }


@dataclass(frozen=True, slots=True)
class MacOSUSBDevice:
    name: str
    vendor_id: str | None = None
    product_id: str | None = None
    serial_number: str | None = None
    location_id: str | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "vendor_id": self.vendor_id,
            "product_id": self.product_id,
            "serial_number": self.serial_number,
            "location_id": self.location_id,
        }


@dataclass(frozen=True, slots=True)
class MacOSSerialPort:
    callout_device: str
    dialin_device: str | None = None
    tty_device: str | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "callout_device": self.callout_device,
            "dialin_device": self.dialin_device,
            "tty_device": self.tty_device,
        }


@dataclass(frozen=True, slots=True)
class MacOSStorageDevice:
    identifier: str
    device_node: str | None = None
    whole: bool = False
    part_of_whole: str | None = None
    media_name: str | None = None
    volume_name: str | None = None
    mount_point: str | None = None
    filesystem: str | None = None
    protocol: str | None = None
    size_bytes: int | None = None
    internal: bool | None = None
    ejectable: bool | None = None
    removable_media: bool | None = None
    read_only_media: bool | None = None
    read_only_volume: bool | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "identifier": self.identifier,
            "device_node": self.device_node,
            "whole": self.whole,
            "part_of_whole": self.part_of_whole,
            "media_name": self.media_name,
            "volume_name": self.volume_name,
            "mount_point": self.mount_point,
            "filesystem": self.filesystem,
            "protocol": self.protocol,
            "size_bytes": self.size_bytes,
            "internal": self.internal,
            "ejectable": self.ejectable,
            "removable_media": self.removable_media,
            "read_only_media": self.read_only_media,
            "read_only_volume": self.read_only_volume,
        }


@dataclass(frozen=True, slots=True)
class MacOSStorageActionResult:
    action: str
    identifier: str
    message: str

    def as_dict(self) -> dict[str, object]:
        return {
            "action": self.action,
            "identifier": self.identifier,
            "message": self.message,
        }
