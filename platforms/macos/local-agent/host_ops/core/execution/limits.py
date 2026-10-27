"""Validated limits for local child-process execution."""

from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ExecutionLimits:
    """Hard bounds applied by :class:`ProcessRunner`."""

    timeout_seconds: float = 30.0
    terminate_grace_seconds: float = 1.0
    pipe_drain_seconds: float = 0.25
    max_stdout_bytes: int = 1_048_576
    max_stderr_bytes: int = 1_048_576

    def __post_init__(self) -> None:
        _positive_finite("timeout_seconds", self.timeout_seconds)
        _positive_finite("terminate_grace_seconds", self.terminate_grace_seconds)
        _positive_finite("pipe_drain_seconds", self.pipe_drain_seconds)
        _positive_int("max_stdout_bytes", self.max_stdout_bytes)
        _positive_int("max_stderr_bytes", self.max_stderr_bytes)


def _positive_finite(name: str, value: float) -> None:
    if (
        isinstance(value, bool)
        or not isinstance(value, int | float)
        or not math.isfinite(value)
        or value <= 0
    ):
        raise ValueError(f"{name} must be a finite number greater than zero")


def _positive_int(name: str, value: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{name} must be an integer greater than zero")
