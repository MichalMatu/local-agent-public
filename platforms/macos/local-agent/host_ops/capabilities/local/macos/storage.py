"""Bounded control of explicitly identified external macOS storage devices."""

from __future__ import annotations

import platform
import plistlib
import re
import time

from local_agent.host_ops.core.execution import ExecutionLimits, ProcessResult, ProcessRunner

from .models import MacOSStorageActionResult

_DISKUTIL = "/usr/sbin/diskutil"
_DISK_IDENTIFIER = re.compile(r"^disk\d+(?:s\d+)?$")
_DEFAULT_LIMITS = ExecutionLimits()


class MacOSStorageControlError(RuntimeError):
    """Raised when a macOS storage action cannot be performed safely."""

    def __init__(self, message: str, *, action_attempted: bool = False) -> None:
        super().__init__(message)
        self.action_attempted = action_attempted


class _OperationBudget:
    def __init__(self, limits: ExecutionLimits) -> None:
        self._limits = limits
        self._deadline = time.monotonic() + limits.timeout_seconds

    def remaining(self) -> ExecutionLimits:
        remaining = self._deadline - time.monotonic()
        if remaining <= 0:
            raise MacOSStorageControlError("macOS storage action exceeded its whole-operation timeout")
        return ExecutionLimits(
            timeout_seconds=remaining,
            terminate_grace_seconds=self._limits.terminate_grace_seconds,
            pipe_drain_seconds=self._limits.pipe_drain_seconds,
            max_stdout_bytes=self._limits.max_stdout_bytes,
            max_stderr_bytes=self._limits.max_stderr_bytes,
        )


class MacOSStorageController:
    """Mount, unmount or eject one explicitly identified external disk/volume."""

    def __init__(
        self,
        runner: ProcessRunner | None = None,
        *,
        system_name: str | None = None,
    ) -> None:
        self._runner = runner or ProcessRunner()
        self._system_name = system_name or platform.system()

    def mount(
        self,
        identifier: str,
        *,
        limits: ExecutionLimits | None = None,
    ) -> MacOSStorageActionResult:
        budget = _OperationBudget(limits or _DEFAULT_LIMITS)
        self._require_external(identifier, budget=budget)
        return self._action("mount", identifier, budget=budget)

    def unmount(
        self,
        identifier: str,
        *,
        limits: ExecutionLimits | None = None,
    ) -> MacOSStorageActionResult:
        budget = _OperationBudget(limits or _DEFAULT_LIMITS)
        self._require_external(identifier, budget=budget)
        return self._action("unmount", identifier, budget=budget)

    def eject(
        self,
        identifier: str,
        *,
        limits: ExecutionLimits | None = None,
    ) -> MacOSStorageActionResult:
        budget = _OperationBudget(limits or _DEFAULT_LIMITS)
        info = self._require_external(identifier, budget=budget)
        if info.get("Whole") is not True:
            raise MacOSStorageControlError("eject requires a whole external disk identifier")
        return self._action("eject", identifier, budget=budget)

    def _require_external(
        self,
        identifier: str,
        *,
        budget: _OperationBudget,
    ) -> dict[object, object]:
        self._require_macos()
        _validate_identifier(identifier)
        result = self._runner.run(
            (_DISKUTIL, "info", "-plist", identifier),
            limits=budget.remaining(),
        )
        payload = _plist(result, f"inspect storage target {identifier}")
        if not isinstance(payload, dict):
            raise MacOSStorageControlError(
                f"inspect storage target {identifier}: unexpected diskutil schema"
            )
        if payload.get("Internal") is not False:
            raise MacOSStorageControlError(
                f"storage target {identifier} is not confirmed external; refusing action"
            )
        return payload

    def _action(
        self,
        action: str,
        identifier: str,
        *,
        budget: _OperationBudget,
    ) -> MacOSStorageActionResult:
        result = self._runner.run(
            (_DISKUTIL, action, identifier),
            limits=budget.remaining(),
        )
        try:
            _require_ok(result, f"{action} storage target {identifier}")
        except MacOSStorageControlError as exc:
            raise MacOSStorageControlError(
                str(exc),
                action_attempted=True,
            ) from exc
        return MacOSStorageActionResult(
            action=action,
            identifier=identifier,
            message=result.stdout.strip(),
        )

    def _require_macos(self) -> None:
        if self._system_name != "Darwin":
            raise MacOSStorageControlError(
                f"macOS storage control requires Darwin, current system is {self._system_name!r}"
            )


def _validate_identifier(identifier: str) -> None:
    if not isinstance(identifier, str) or not _DISK_IDENTIFIER.fullmatch(identifier):
        raise MacOSStorageControlError("storage identifier must match diskN or diskNsN")


def _plist(result: ProcessResult, action: str) -> object:
    _require_ok(result, action)
    try:
        return plistlib.loads(result.stdout.encode("utf-8"))
    except (plistlib.InvalidFileException, ValueError) as exc:
        raise MacOSStorageControlError(f"{action}: invalid plist output") from exc


def _require_ok(result: ProcessResult, action: str) -> None:
    if result.ok and not result.stdout_truncated:
        return
    if result.stdout_truncated:
        raise MacOSStorageControlError(f"{action}: command stdout was truncated")
    detail = result.stderr.strip() or result.error or f"exit_code={result.exit_code}"
    raise MacOSStorageControlError(f"{action}: {detail}")
