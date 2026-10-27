"""One bounded, opt-in diagnostic view of host resources and named tools."""
from __future__ import annotations

import time
from collections.abc import Sequence

from local_agent.host_ops.capabilities.local.tools import ToolInspector
from local_agent.host_ops.capabilities.local.tools.inspection import _tool_names
from local_agent.host_ops.core.execution import ExecutionLimits

from .capacity import HostCapacityProfiler
from .profile import HostProfileError, HostProfiler

_DEFAULT_LIMITS = ExecutionLimits(timeout_seconds=15.0, max_stdout_bytes=8192, max_stderr_bytes=8192)
DEFAULT_TOOL_NAMES = ("git", "python3", "node", "cmake", "ninja")


class HostOverviewCollector:
    """Compose existing read-only capabilities; never grant tasks or reserve capacity."""

    def __init__(
        self,
        *,
        host: HostProfiler | None = None,
        capacity: HostCapacityProfiler | None = None,
        tools: ToolInspector | None = None,
    ) -> None:
        self._host = host or HostProfiler()
        self._capacity = capacity or HostCapacityProfiler()
        self._tools = tools or ToolInspector()

    def inspect(
        self,
        *,
        names: Sequence[str] = DEFAULT_TOOL_NAMES,
        limits: ExecutionLimits | None = None,
    ) -> dict[str, object]:
        active = limits or _DEFAULT_LIMITS
        if not 0 < active.timeout_seconds <= 120:
            raise ValueError("overview timeout must be between 0 and 120 seconds")
        # Reuse the existing tool policy, validating before any resource probes.
        unique = _tool_names(names)
        if len(unique) > 12:
            raise ValueError("overview supports at most 12 named tools")
        deadline = time.monotonic() + active.timeout_seconds

        def remaining() -> ExecutionLimits:
            left = deadline - time.monotonic()
            if left <= 0:
                raise HostProfileError("host overview exceeded its whole-operation timeout")
            return ExecutionLimits(
                timeout_seconds=left,
                terminate_grace_seconds=active.terminate_grace_seconds,
                pipe_drain_seconds=active.pipe_drain_seconds,
                max_stdout_bytes=active.max_stdout_bytes,
                max_stderr_bytes=active.max_stderr_bytes,
            )

        profile = self._host.inspect(limits=remaining())
        capacity = self._capacity.inspect(limits=remaining())
        tool_records = []
        for name in unique:
            item = self._tools.inspect((name,), limits=remaining())[0]
            # Resolved paths are intentionally excluded from this overview
            # (individual opt-in tool inspections retain full detail).
            tool_records.append({
                "name": item.name,
                "present": item.present,
                "version": item.version,
                "version_exit_code": item.version_exit_code,
                "error": "version_probe_failed" if item.error else None,
            })
        remaining()  # Explicit timeout even if the last probe consumed the budget.
        return {
            "contract_version": 1,
            "profile": profile.as_dict(),
            "capacity": capacity.as_dict(),
            "tools": tool_records,
            "observed_at_utc": capacity.observed_at_utc,
            "interpretation": "diagnostic_only_not_reserved_capacity",
        }
