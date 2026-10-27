"""Best-effort read-only GPU identity discovery for the local host profile."""

from __future__ import annotations

import json

from local_agent.host_ops.core.execution import ExecutionLimits, ProcessRunner

_SYSTEM_PROFILER = "/usr/sbin/system_profiler"
_DISPLAY_DATA_TYPE = "SPDisplaysDataType"


def discover_gpu_devices(
    system: str,
    *,
    runner: ProcessRunner,
    limits: ExecutionLimits,
) -> tuple[str, ...] | None:
    """Return stable GPU names when the current platform exposes them safely."""
    if system != "Darwin":
        return None

    result = runner.run(
        (
            _SYSTEM_PROFILER,
            "-json",
            "-detailLevel",
            "mini",
            _DISPLAY_DATA_TYPE,
        ),
        limits=limits,
    )
    if not result.ok or result.stdout_truncated or result.stderr_truncated:
        return None

    try:
        payload: object = json.loads(result.stdout)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None

    raw_devices = payload.get(_DISPLAY_DATA_TYPE)
    if not isinstance(raw_devices, list):
        return None

    devices: list[str] = []
    for raw_device in raw_devices:
        name = _gpu_name(raw_device)
        if name is not None and name not in devices:
            devices.append(name)
    return tuple(devices)


def _gpu_name(raw_device: object) -> str | None:
    if not isinstance(raw_device, dict):
        return None
    for key in ("sppci_model", "_name"):
        value = raw_device.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None
