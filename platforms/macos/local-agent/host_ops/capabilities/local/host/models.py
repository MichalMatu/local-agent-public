"""Structured local host capability facts."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class HostProfile:
    hostname: str
    system: str
    release: str
    architecture: str
    logical_cpu_count: int | None
    memory_total_bytes: int | None
    root_total_bytes: int
    root_free_bytes: int
    gpu_devices: tuple[str, ...] | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "hostname": self.hostname,
            "system": self.system,
            "release": self.release,
            "architecture": self.architecture,
            "logical_cpu_count": self.logical_cpu_count,
            "memory_total_bytes": self.memory_total_bytes,
            "gpu_devices": list(self.gpu_devices) if self.gpu_devices is not None else None,
            "root_total_bytes": self.root_total_bytes,
            "root_free_bytes": self.root_free_bytes,
        }


@dataclass(frozen=True, slots=True)
class HostCapacitySnapshot:
    """Ephemeral diagnostic capacity; never an execution admission decision."""

    observed_at_utc: str
    system: str
    logical_cpu_count: int | None
    memory_available_bytes: int | None
    memory_availability_kind: str | None
    load_average_1m: float | None
    load_average_5m: float | None
    load_average_15m: float | None
    root_total_bytes: int
    root_free_bytes: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "observed_at_utc": self.observed_at_utc,
            "system": self.system,
            "logical_cpu_count": self.logical_cpu_count,
            "memory_available_bytes": self.memory_available_bytes,
            "memory_availability_kind": self.memory_availability_kind,
            "load_average_1m": self.load_average_1m,
            "load_average_5m": self.load_average_5m,
            "load_average_15m": self.load_average_15m,
            "root_total_bytes": self.root_total_bytes,
            "root_free_bytes": self.root_free_bytes,
        }
