"""Bounded read-only *current* host capacity observations.

This is diagnostic evidence, not free execution slots or task admission.
"""
from __future__ import annotations

import math
import os
import platform
import re
import shutil
from datetime import datetime, timezone

from local_agent.host_ops.core.execution import ExecutionLimits, ProcessRunner

from .models import HostCapacitySnapshot
from .profile import HostProfileError

_VM_STAT = "/usr/bin/vm_stat"
_MAX_MEMINFO_BYTES = 16_384
_DEFAULT_LIMITS = ExecutionLimits(
    timeout_seconds=5.0,
    max_stdout_bytes=8192,
    max_stderr_bytes=8192,
)
_PAGE_SIZE = re.compile(r"page size of (\d+) bytes")
_PAGE_LINE = re.compile(r"^Pages (free|inactive|speculative):\s*(\d+)\.\s*$")


class HostCapacityProfiler:
    """Collect ephemeral resource facts without checking authorization or scheduling."""

    def __init__(self, runner: ProcessRunner | None = None) -> None:
        self._runner = runner or ProcessRunner()

    def inspect(self, *, limits: ExecutionLimits | None = None) -> HostCapacitySnapshot:
        active_limits = limits or _DEFAULT_LIMITS
        if not 0 < active_limits.timeout_seconds <= 3600:
            raise ValueError("capacity timeout must be between 0 and 3600 seconds")
        system = platform.system()
        try:
            disk = shutil.disk_usage("/")
        except OSError as exc:
            raise HostProfileError("could not inspect root filesystem capacity") from exc
        memory_available: int | None = None
        memory_kind: str | None = None
        if system == "Linux":
            memory_available = _linux_mem_available_bytes()
            if memory_available is not None:
                memory_kind = "linux_memavailable"
        elif system == "Darwin":
            memory_available = _macos_available_estimate(self._runner, active_limits)
            if memory_available is not None:
                memory_kind = "macos_vm_stat_estimate"
        load = _load_average()
        return HostCapacitySnapshot(
            observed_at_utc=datetime.now(timezone.utc).isoformat(),
            system=system,
            logical_cpu_count=os.cpu_count(),
            memory_available_bytes=memory_available,
            memory_availability_kind=memory_kind,
            load_average_1m=load[0],
            load_average_5m=load[1],
            load_average_15m=load[2],
            root_total_bytes=disk.total,
            root_free_bytes=disk.free,
        )


def _load_average() -> tuple[float | None, float | None, float | None]:
    try:
        values = os.getloadavg()
    except (AttributeError, OSError):
        return (None, None, None)
    if len(values) != 3 or any(not math.isfinite(x) or x < 0 for x in values):
        return (None, None, None)
    return values


def _linux_mem_available_bytes() -> int | None:
    """Use only the kernel's MemAvailable estimate, not misleading MemFree."""
    try:
        with open("/proc/meminfo", "rb") as stream:
            data = stream.read(_MAX_MEMINFO_BYTES + 1)
    except OSError:
        return None
    if len(data) > _MAX_MEMINFO_BYTES:
        return None
    for line in data.splitlines():
        if not line.startswith(b"MemAvailable:"):
            continue
        match = re.fullmatch(rb"MemAvailable:\s+(\d+) kB\s*", line)
        if match is None:
            return None
        kib = int(match.group(1))
        return kib * 1024 if 0 <= kib <= ((2**63 - 1) // 1024) else None
    return None


def _macos_available_estimate(runner: ProcessRunner, limits: ExecutionLimits) -> int | None:
    """Best-effort reclaimable-page estimate, NOT guaranteed allocatable memory."""
    result = runner.run((_VM_STAT,), limits=limits)
    if not result.ok or result.stdout_truncated or result.stderr_truncated:
        return None
    lines = result.stdout.splitlines()
    if not lines:
        return None
    page_match = _PAGE_SIZE.search(lines[0])
    if page_match is None:
        return None
    page_size = int(page_match.group(1))
    if not (1024 <= page_size <= 65536):
        return None
    pages: dict[str, int] = {}
    for line in lines[1:]:
        match = _PAGE_LINE.fullmatch(line.strip())
        if match is not None:
            if match.group(1) in pages:
                return None
            pages[match.group(1)] = int(match.group(2))
    if "free" not in pages or "inactive" not in pages:
        return None
    count = pages["free"] + pages["inactive"] + pages.get("speculative", 0)
    return count * page_size if 0 <= count <= ((2**63 - 1) // page_size) else None
