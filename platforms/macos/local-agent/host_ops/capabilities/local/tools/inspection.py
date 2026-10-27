"""Bounded inspection of explicitly named local executables."""

from __future__ import annotations

import os
import re
import shutil
from collections.abc import Sequence

from local_agent.host_ops.core.execution import ExecutionLimits, ProcessResult, ProcessRunner, ProcessState

from .models import ToolInspectionResult

_TOOL_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,127}$")
_DEFAULT_LIMITS = ExecutionLimits(
    timeout_seconds=5.0,
    max_stdout_bytes=8192,
    max_stderr_bytes=8192,
)


class ToolInspectionError(ValueError):
    """Raised when a local tool inspection request is invalid."""


class ToolInspector:
    """Inspect PATH-resolved executable presence and bounded version evidence."""

    def __init__(self, runner: ProcessRunner | None = None) -> None:
        self._runner = runner or ProcessRunner()

    def inspect(
        self,
        names: Sequence[str],
        *,
        limits: ExecutionLimits | None = None,
    ) -> tuple[ToolInspectionResult, ...]:
        normalized = _tool_names(names)
        active_limits = limits or _DEFAULT_LIMITS
        return tuple(self._inspect_one(name, limits=active_limits) for name in normalized)

    def _inspect_one(self, name: str, *, limits: ExecutionLimits) -> ToolInspectionResult:
        resolved = shutil.which(name)
        if resolved is None:
            return ToolInspectionResult(name=name, present=False)

        resolved_path = os.path.realpath(resolved)
        version_flag = "-V" if name == "ssh" else "--version"
        result = self._runner.run((resolved_path, version_flag), limits=limits)
        version = _first_output_line(result)
        error = _version_error(result)
        return ToolInspectionResult(
            name=name,
            present=True,
            resolved_path=resolved_path,
            version=version,
            version_exit_code=result.exit_code,
            error=error,
        )


def _tool_names(names: Sequence[str]) -> tuple[str, ...]:
    if not names:
        raise ToolInspectionError("at least one tool name is required")
    if len(names) > 64:
        raise ToolInspectionError("at most 64 tool names may be inspected at once")

    unique: list[str] = []
    seen: set[str] = set()
    for name in names:
        if not isinstance(name, str) or _TOOL_NAME.fullmatch(name) is None:
            raise ToolInspectionError(
                f"invalid tool name {name!r}; expected one executable basename"
            )
        if name not in seen:
            seen.add(name)
            unique.append(name)
    return tuple(unique)


def _first_output_line(result: ProcessResult) -> str | None:
    for stream in (result.stdout, result.stderr):
        for line in stream.splitlines():
            value = line.strip()
            if value:
                return value
    return None


def _version_error(result: ProcessResult) -> str | None:
    if result.stdout_truncated or result.stderr_truncated:
        return "version output was truncated"
    if result.ok:
        return None
    if result.error:
        return result.error
    if result.state is not ProcessState.COMPLETED:
        return f"version probe {result.state.value}"
    return f"version probe exited with code {result.exit_code}"
