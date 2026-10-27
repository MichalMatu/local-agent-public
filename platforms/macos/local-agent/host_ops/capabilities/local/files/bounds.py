"""Shared bounds for local artifact hashing and copying."""

from __future__ import annotations

import time

from local_agent.host_ops.core.execution import ExecutionLimits

DEFAULT_MAX_ARTIFACT_BYTES = 512 * 1024 * 1024
MAX_ARTIFACT_BYTES = 16 * 1024 * 1024 * 1024
DEFAULT_ARTIFACT_TIMEOUT_SECONDS = 300.0


class ArtifactOperationTimeout(RuntimeError):
    """Raised internally when one local artifact operation exhausts its deadline."""


class ArtifactOperationBudget:
    """One monotonic deadline shared by all local I/O stages of an artifact operation."""

    def __init__(self, limits: ExecutionLimits | None = None) -> None:
        self._limits = limits or ExecutionLimits(
            timeout_seconds=DEFAULT_ARTIFACT_TIMEOUT_SECONDS
        )
        self._deadline = time.monotonic() + self._limits.timeout_seconds

    def checkpoint(self) -> None:
        if self._deadline - time.monotonic() <= 0:
            raise ArtifactOperationTimeout


def validate_max_artifact_bytes(value: int) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 1 <= value <= MAX_ARTIFACT_BYTES
    ):
        raise ValueError(
            f"max_bytes must be an integer in range 1..{MAX_ARTIFACT_BYTES}"
        )
    return value
