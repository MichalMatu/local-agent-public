"""Read-only generic local host capability profile."""

from __future__ import annotations

import os
import platform
import shutil
import socket
import time

from local_agent.host_ops.core.execution import ExecutionLimits, ProcessResult, ProcessRunner

from .gpu import discover_gpu_devices
from .models import HostProfile

_SYSCTL = "/usr/sbin/sysctl"
_DEFAULT_LIMITS = ExecutionLimits(
    timeout_seconds=5.0,
    max_stdout_bytes=8192,
    max_stderr_bytes=8192,
)


class HostProfileError(RuntimeError):
    """Raised when trustworthy local host facts cannot be produced."""


class _OperationBudget:
    def __init__(self, limits: ExecutionLimits) -> None:
        self._limits = limits
        self._deadline = time.monotonic() + limits.timeout_seconds

    def remaining(self) -> ExecutionLimits:
        remaining = self._deadline - time.monotonic()
        if remaining <= 0:
            raise HostProfileError("host profile exceeded its whole-operation timeout")
        return ExecutionLimits(
            timeout_seconds=remaining,
            terminate_grace_seconds=self._limits.terminate_grace_seconds,
            pipe_drain_seconds=self._limits.pipe_drain_seconds,
            max_stdout_bytes=self._limits.max_stdout_bytes,
            max_stderr_bytes=self._limits.max_stderr_bytes,
        )


class HostProfiler:
    """Collect bounded vendor-neutral facts about the local execution host."""

    def __init__(self, runner: ProcessRunner | None = None) -> None:
        self._runner = runner or ProcessRunner()

    def inspect(self, *, limits: ExecutionLimits | None = None) -> HostProfile:
        hostname = socket.gethostname().strip()
        system = platform.system().strip()
        release = platform.release().strip()
        architecture = platform.machine().strip()
        if not hostname or not system or not release or not architecture:
            raise HostProfileError("local host identity returned an empty required field")

        try:
            disk = shutil.disk_usage("/")
        except OSError as exc:
            raise HostProfileError(f"could not inspect root filesystem capacity: {exc}") from exc

        effective_limits = limits or _DEFAULT_LIMITS
        if system == "Darwin":
            budget = _OperationBudget(effective_limits)
            memory_total_bytes = self._memory_total_bytes(
                system,
                limits=budget.remaining(),
            )
            gpu_devices = discover_gpu_devices(
                system,
                runner=self._runner,
                limits=budget.remaining(),
            )
        else:
            memory_total_bytes = self._memory_total_bytes(
                system,
                limits=effective_limits,
            )
            gpu_devices = discover_gpu_devices(
                system,
                runner=self._runner,
                limits=effective_limits,
            )

        return HostProfile(
            hostname=hostname,
            system=system,
            release=release,
            architecture=architecture,
            logical_cpu_count=os.cpu_count(),
            memory_total_bytes=memory_total_bytes,
            gpu_devices=gpu_devices,
            root_total_bytes=disk.total,
            root_free_bytes=disk.free,
        )

    def _memory_total_bytes(
        self,
        system: str,
        *,
        limits: ExecutionLimits,
    ) -> int | None:
        if system == "Darwin":
            result = self._runner.run((_SYSCTL, "-n", "hw.memsize"), limits=limits)
            return _positive_integer_output(result, "read macOS physical memory")
        if system == "Linux":
            return _linux_memory_total_bytes()
        return None


def _linux_memory_total_bytes() -> int | None:
    try:
        pages = os.sysconf("SC_PHYS_PAGES")
        page_size = os.sysconf("SC_PAGE_SIZE")
    except (OSError, ValueError):
        return None
    if (
        isinstance(pages, int)
        and not isinstance(pages, bool)
        and pages > 0
        and isinstance(page_size, int)
        and not isinstance(page_size, bool)
        and page_size > 0
    ):
        return pages * page_size
    return None


def _positive_integer_output(result: ProcessResult, action: str) -> int:
    if not result.ok or result.stdout_truncated or result.stderr_truncated:
        detail = result.stderr.strip() or result.error or f"exit_code={result.exit_code}"
        if result.stdout_truncated or result.stderr_truncated:
            detail = "command output was truncated"
        raise HostProfileError(f"{action}: {detail}")
    value = result.stdout.strip()
    try:
        parsed = int(value)
    except ValueError as exc:
        raise HostProfileError(f"{action}: invalid integer output") from exc
    if parsed <= 0:
        raise HostProfileError(f"{action}: expected a positive integer")
    return parsed
