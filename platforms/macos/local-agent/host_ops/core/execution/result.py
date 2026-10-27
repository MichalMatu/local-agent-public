"""Structured process execution results."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any


class ProcessState(StrEnum):
    COMPLETED = "completed"
    LINGERING_DESCENDANTS = "lingering_descendants"
    SPAWN_FAILED = "spawn_failed"
    TIMED_OUT = "timed_out"
    OUTPUT_LIMIT_EXCEEDED = "output_limit_exceeded"
    OUTPUT_SINK_FAILED = "output_sink_failed"


@dataclass(frozen=True, slots=True)
class ProcessResult:
    state: ProcessState
    exit_code: int | None
    stdout: str
    stderr: str
    duration_seconds: float
    stdout_truncated: bool = False
    stderr_truncated: bool = False
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.state is ProcessState.COMPLETED and self.exit_code == 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "state": self.state.value,
            "exit_code": self.exit_code,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "duration_seconds": self.duration_seconds,
            "stdout_truncated": self.stdout_truncated,
            "stderr_truncated": self.stderr_truncated,
            "error": self.error,
        }
