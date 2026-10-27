"""CLI rendering for macOS host, device and storage operations."""

from __future__ import annotations

import json
import sys
from collections.abc import Mapping, Sequence

from local_agent.host_ops.capabilities.local.macos import (
    MacOSInspectionError,
    MacOSInspector,
    MacOSStorageControlError,
    MacOSStorageController,
)


def run_host(*, as_json: bool) -> int:
    try:
        info = MacOSInspector().host_info()
    except MacOSInspectionError as exc:
        return _inspection_error(exc, as_json=as_json)
    payload = info.as_dict()
    if as_json:
        print(json.dumps(payload, sort_keys=True))
    else:
        print(f"macOS {info.product_version} ({info.architecture})")
    return 0


def run_usb(*, as_json: bool) -> int:
    try:
        devices = MacOSInspector().usb_devices()
    except MacOSInspectionError as exc:
        return _inspection_error(exc, as_json=as_json)
    payload = [device.as_dict() for device in devices]
    return _render_rows(payload, as_json=as_json, empty_label="No USB devices found")


def run_serial(*, as_json: bool) -> int:
    try:
        ports = MacOSInspector().serial_ports()
    except MacOSInspectionError as exc:
        return _inspection_error(exc, as_json=as_json)
    payload = [port.as_dict() for port in ports]
    return _render_rows(payload, as_json=as_json, empty_label="No serial ports found")


def run_storage(*, as_json: bool) -> int:
    try:
        devices = MacOSInspector().external_storage()
    except MacOSInspectionError as exc:
        return _inspection_error(exc, as_json=as_json)
    payload = [device.as_dict() for device in devices]
    return _render_rows(payload, as_json=as_json, empty_label="No external physical storage found")


def run_storage_action(action: str, identifier: str, *, as_json: bool) -> int:
    controller = MacOSStorageController()
    try:
        if action == "mount":
            result = controller.mount(identifier)
        elif action == "unmount":
            result = controller.unmount(identifier)
        elif action == "eject":
            result = controller.eject(identifier)
        else:
            raise AssertionError(f"unsupported storage action: {action}")
    except MacOSStorageControlError as exc:
        if as_json:
            print(json.dumps({"error": str(exc)}, sort_keys=True), file=sys.stderr)
        else:
            print(f"macOS storage action failed: {exc}", file=sys.stderr)
        return 1

    if as_json:
        print(json.dumps(result.as_dict(), sort_keys=True))
    else:
        print(f"{result.action} {result.identifier}: {result.message}".rstrip())
    return 0


def _render_rows(
    rows: Sequence[Mapping[str, object]],
    *,
    as_json: bool,
    empty_label: str,
) -> int:
    if as_json:
        print(json.dumps(list(rows), sort_keys=True))
        return 0
    if not rows:
        print(empty_label)
        return 0
    for row in rows:
        print(" ".join(f"{key}={value}" for key, value in row.items() if value is not None))
    return 0


def _inspection_error(exc: MacOSInspectionError, *, as_json: bool) -> int:
    if as_json:
        print(json.dumps({"error": str(exc)}, sort_keys=True), file=sys.stderr)
    else:
        print(f"macOS inspection failed: {exc}", file=sys.stderr)
    return 1
