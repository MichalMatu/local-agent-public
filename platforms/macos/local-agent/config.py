"""Startup-loaded Local Agent runtime configuration."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Mapping

MIN_TIMEOUT = 1
MAX_STOP_GRACE_SECONDS = 3600


@dataclass(frozen=True)
class TimeoutConfig:
    command_default: int
    command_max: int
    idle_default: int
    idle_max: int
    task_default: int
    task_max: int
    stop_grace: int


_DEFAULTS = {
    "command_default": 900,
    "command_max": 7200,
    "idle_default": 300,
    "idle_max": 3600,
    "task_default": 1800,
    "task_max": 21600,
    "stop_grace": 300,
}

_ENV_NAMES = {
    "command_default": "LOCAL_AGENT_COMMAND_TIMEOUT_DEFAULT",
    "command_max": "LOCAL_AGENT_COMMAND_TIMEOUT_MAX",
    "idle_default": "LOCAL_AGENT_IDLE_TIMEOUT_DEFAULT",
    "idle_max": "LOCAL_AGENT_IDLE_TIMEOUT_MAX",
    "task_default": "LOCAL_AGENT_TASK_TIMEOUT_DEFAULT",
    "task_max": "LOCAL_AGENT_TASK_TIMEOUT_MAX",
    "stop_grace": "LOCAL_AGENT_STOP_GRACE_SECONDS",
}


def _read_positive_int(name: str, raw: str) -> int:
    try:
        value = int(raw.strip())
    except (AttributeError, ValueError):
        raise ValueError(f"{name} must be an integer, got {raw!r}") from None
    if value < MIN_TIMEOUT:
        raise ValueError(f"{name} must be at least {MIN_TIMEOUT} second, got {value}")
    return value


def load_timeout_config(environ: Mapping[str, str] | None = None) -> TimeoutConfig:
    source = os.environ if environ is None else environ
    values: dict[str, int] = {}
    for field, name in _ENV_NAMES.items():
        raw = source.get(name)
        values[field] = _DEFAULTS[field] if raw is None else _read_positive_int(name, raw)

    for kind in ("command", "idle", "task"):
        default = values[f"{kind}_default"]
        maximum = values[f"{kind}_max"]
        if default > maximum:
            raise ValueError(
                f"{_ENV_NAMES[f'{kind}_default']} ({default}) cannot exceed "
                f"{_ENV_NAMES[f'{kind}_max']} ({maximum})"
            )
    if values["stop_grace"] > MAX_STOP_GRACE_SECONDS:
        raise ValueError(
            f"LOCAL_AGENT_STOP_GRACE_SECONDS cannot exceed {MAX_STOP_GRACE_SECONDS}"
        )
    return TimeoutConfig(**values)


TIMEOUTS = load_timeout_config()


@dataclass(frozen=True)
class MemoryLimitConfig:
    minimum_mb: int
    default_mb: int
    maximum_mb: int


def load_memory_limit_config(environ: Mapping[str, str] | None = None) -> MemoryLimitConfig:
    source = os.environ if environ is None else environ
    values = {}
    for field, suffix, default in (
        ("minimum_mb", "MIN", 0), ("default_mb", "DEFAULT", 4096),
        ("maximum_mb", "MAX", 16384),
    ):
        name = f"LOCAL_AGENT_MEMORY_LIMIT_MB_{suffix}"
        raw = source.get(name, str(default))
        try:
            value = int(raw.strip())
        except (AttributeError, ValueError):
            raise ValueError(f"{name} must be an integer") from None
        if not 0 <= value <= 16384:
            raise ValueError(f"{name} must be in range 0..16384 MiB")
        values[field] = value
    if not values["minimum_mb"] <= values["default_mb"] <= values["maximum_mb"] or values["maximum_mb"] == 0:
        raise ValueError("memory limits require minimum <= default <= maximum and maximum > 0")
    return MemoryLimitConfig(**values)


MEMORY_LIMITS = load_memory_limit_config()
