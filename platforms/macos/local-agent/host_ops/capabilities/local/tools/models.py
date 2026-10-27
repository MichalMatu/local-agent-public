"""Structured results for local executable inspection."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ToolInspectionResult:
    name: str
    present: bool
    resolved_path: str | None = None
    version: str | None = None
    version_exit_code: int | None = None
    error: str | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "present": self.present,
            "resolved_path": self.resolved_path,
            "version": self.version,
            "version_exit_code": self.version_exit_code,
            "error": self.error,
        }
