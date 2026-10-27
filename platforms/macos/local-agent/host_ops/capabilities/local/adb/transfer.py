"""Bounded verified file transfer for one explicit ADB device."""

from __future__ import annotations

import errno
import hashlib
import os
import re
import secrets
import shutil
import stat
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from local_agent.host_ops.core.execution import ExecutionLimits, ProcessRunner

from .models import AdbTransferResult
from .remote_files import AdbRemoteFiles, AdbTransferError, normalize_remote_file_path

DEFAULT_MAX_TRANSFER_BYTES = 512 * 1024 * 1024
MAX_TRANSFER_BYTES = 16 * 1024 * 1024 * 1024
_HASH_CHUNK_BYTES = 1024 * 1024
_SERIAL_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,254}$")
_UNSUPPORTED_DIRECTORY_FSYNC = {errno.EINVAL, errno.EBADF}
if hasattr(errno, "ENOTSUP"):
    _UNSUPPORTED_DIRECTORY_FSYNC.add(errno.ENOTSUP)


class AdbFileTransfer:
    """Push or pull one verified regular file for one explicit ADB serial."""

    def __init__(
        self,
        runner: ProcessRunner | None = None,
        *,
        resolver: Callable[[str], str | None] = shutil.which,
    ) -> None:
        self._runner = runner or ProcessRunner()
        self._resolver = resolver

    def push(
        self,
        serial: str,
        local_source: Path,
        remote_destination: str,
        *,
        replace: bool = False,
        max_bytes: int = DEFAULT_MAX_TRANSFER_BYTES,
        limits: ExecutionLimits | None = None,
    ) -> AdbTransferResult:
        maximum = _validate_max_bytes(max_bytes)
        budget = _TransferBudget(limits or ExecutionLimits(timeout_seconds=300.0))
        normalized_serial = _normalize_serial(serial)
        source = _inspect_local_source(local_source, maximum)
        destination = normalize_remote_file_path(remote_destination)
        remote = self._remote(normalized_serial, budget)
        remote.require_ready()
        remote.require_directory(str(PurePosixPath(destination).parent))
        replaced_existing = remote.validate_destination(destination, replace=replace)
        stage = str(PurePosixPath(destination).parent / f".hostops-adb-{secrets.token_hex(12)}.tmp")
        stage_present = False
        stage_may_exist = False
        action_attempted = False
        committed = False

        try:
            action_attempted = True
            stage_may_exist = True
            remote.push(source.path, stage)
            stage_present = True
            remote_size = remote.size(stage)
            remote_sha256 = remote.sha256(stage)
            if remote_size != source.size_bytes or remote_sha256 != source.sha256:
                raise AdbTransferError("uploaded staging file failed size or SHA-256 verification")
            if _sha256_file(source.path) != source.sha256:
                raise AdbTransferError("local source changed while ADB push was in progress")

            remote.commit_stage(stage, destination, replace=replace)
            committed = True
            stage_present = False
            remote.require_regular_file(destination, label="destination")
            if remote.size(destination) != source.size_bytes:
                raise AdbTransferError("remote destination failed post-commit size verification")
            if remote.sha256(destination) != source.sha256:
                raise AdbTransferError("remote destination failed post-commit SHA-256 verification")
            return AdbTransferResult(
                direction="push",
                serial=normalized_serial,
                source=str(source.path),
                destination=destination,
                size_bytes=source.size_bytes,
                sha256=source.sha256,
                replaced_existing=replaced_existing,
            )
        except (AdbTransferError, OSError, ValueError) as exc:
            cleanup_failed = False
            if stage_present or (stage_may_exist and not committed):
                try:
                    cleanup_failed = not remote.cleanup(stage, best_effort=True)
                except (AdbTransferError, OSError, ValueError):
                    cleanup_failed = True
            raise _adb_transfer_error(
                exc,
                action_attempted=action_attempted,
                committed=committed,
                cleanup_failed=cleanup_failed,
            ) from exc

    def pull(
        self,
        serial: str,
        remote_source: str,
        local_destination: Path,
        *,
        replace: bool = False,
        max_bytes: int = DEFAULT_MAX_TRANSFER_BYTES,
        limits: ExecutionLimits | None = None,
    ) -> AdbTransferResult:
        maximum = _validate_max_bytes(max_bytes)
        budget = _TransferBudget(limits or ExecutionLimits(timeout_seconds=300.0))
        normalized_serial = _normalize_serial(serial)
        source = normalize_remote_file_path(remote_source)
        remote = self._remote(normalized_serial, budget)
        remote.require_ready()
        remote.require_regular_file(source, label="source")
        remote_size = remote.size(source)
        if remote_size > maximum:
            raise AdbTransferError(f"remote source exceeds max_bytes ({remote_size} > {maximum})")
        remote_sha256 = remote.sha256(source)
        destination, replaced_existing = _prepare_local_destination(
            local_destination,
            replace=replace,
        )
        stage = destination.parent / f".hostops-adb-{secrets.token_hex(12)}.tmp"
        action_attempted = False
        committed = False
        failure: AdbTransferError | OSError | ValueError | None = None
        result: AdbTransferResult | None = None

        try:
            action_attempted = True
            remote.pull(source, stage)
            staged = _inspect_local_source(stage, maximum)
            if staged.size_bytes != remote_size or staged.sha256 != remote_sha256:
                raise AdbTransferError(
                    "downloaded staging file failed size or SHA-256 verification"
                )
            if remote.size(source) != remote_size or remote.sha256(source) != remote_sha256:
                raise AdbTransferError("remote source changed while ADB pull was in progress")
            _fsync_file(stage)
            _commit_local_stage(stage, destination, replace=replace)
            committed = True
            directory_synced = _sync_directory(destination.parent)
            result = AdbTransferResult(
                direction="pull",
                serial=normalized_serial,
                source=source,
                destination=str(destination),
                size_bytes=remote_size,
                sha256=remote_sha256,
                replaced_existing=replaced_existing,
                local_directory_synced=directory_synced,
            )
        except (AdbTransferError, OSError, ValueError) as exc:
            failure = exc

        cleanup_failed = False
        if stage.exists() or stage.is_symlink():
            try:
                stage.unlink(missing_ok=True)
            except OSError:
                cleanup_failed = True

        if failure is not None:
            raise _adb_transfer_error(
                failure,
                action_attempted=action_attempted,
                committed=committed,
                cleanup_failed=cleanup_failed,
            ) from failure
        if cleanup_failed:
            raise AdbTransferError(
                "local staging cleanup failed after ADB pull",
                action_attempted=action_attempted,
                committed=committed,
                cleanup_failed=True,
            )
        assert result is not None
        return result

    def _remote(self, serial: str, budget: _TransferBudget) -> AdbRemoteFiles:
        return AdbRemoteFiles(
            self._runner,
            self._resolve_executable(),
            serial,
            budget.remaining,
        )

    def _resolve_executable(self) -> str:
        resolved = self._resolver("adb")
        if resolved is None:
            raise AdbTransferError("adb executable was not found on PATH")
        path = Path(os.path.realpath(os.path.expanduser(resolved)))
        if not path.is_absolute():
            raise AdbTransferError("resolved adb executable path is not absolute")
        return str(path)


def _adb_transfer_error(
    exc: AdbTransferError | OSError | ValueError,
    *,
    action_attempted: bool,
    committed: bool,
    cleanup_failed: bool,
) -> AdbTransferError:
    if isinstance(exc, AdbTransferError):
        action_attempted = action_attempted or exc.action_attempted
        committed = committed or exc.committed
        cleanup_failed = cleanup_failed or exc.cleanup_failed
    return AdbTransferError(
        str(exc),
        action_attempted=action_attempted or committed,
        committed=committed,
        cleanup_failed=cleanup_failed,
    )


@dataclass(frozen=True, slots=True)
class _LocalSource:
    path: Path
    size_bytes: int
    sha256: str


class _TransferBudget:
    def __init__(self, limits: ExecutionLimits) -> None:
        self._limits = limits
        self._deadline = time.monotonic() + limits.timeout_seconds

    def remaining(self) -> ExecutionLimits:
        remaining = self._deadline - time.monotonic()
        if remaining <= 0:
            raise AdbTransferError("ADB file transfer exceeded its whole-operation timeout")
        return ExecutionLimits(
            timeout_seconds=remaining,
            terminate_grace_seconds=self._limits.terminate_grace_seconds,
            pipe_drain_seconds=self._limits.pipe_drain_seconds,
            max_stdout_bytes=self._limits.max_stdout_bytes,
            max_stderr_bytes=self._limits.max_stderr_bytes,
        )


def _normalize_serial(serial: str) -> str:
    if not isinstance(serial, str) or _SERIAL_PATTERN.fullmatch(serial) is None:
        raise AdbTransferError("ADB serial contains unsupported characters")
    return serial


def _validate_max_bytes(value: int) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 1 <= value <= MAX_TRANSFER_BYTES
    ):
        raise AdbTransferError(f"max_bytes must be an integer in range 1..{MAX_TRANSFER_BYTES}")
    return value


def _inspect_local_source(path: Path, max_bytes: int) -> _LocalSource:
    source = Path(path).expanduser()
    if source.is_symlink():
        raise AdbTransferError("local source must not be a symbolic link")
    try:
        resolved = source.resolve(strict=True)
        metadata = resolved.stat()
    except OSError as exc:
        raise AdbTransferError(f"could not inspect local source: {exc}") from exc
    if not stat.S_ISREG(metadata.st_mode):
        raise AdbTransferError("local source is not a regular file")
    if metadata.st_size > max_bytes:
        raise AdbTransferError(f"local source exceeds max_bytes ({metadata.st_size} > {max_bytes})")
    try:
        digest = _sha256_file(resolved)
    except OSError as exc:
        raise AdbTransferError(f"could not hash local source: {exc}") from exc
    return _LocalSource(resolved, metadata.st_size, digest)


def _prepare_local_destination(path: Path, *, replace: bool) -> tuple[Path, bool]:
    requested = Path(path).expanduser()
    if requested.name in {"", ".", ".."}:
        raise AdbTransferError("local destination must identify one file")
    parent = requested.parent
    if parent.is_symlink():
        raise AdbTransferError("local destination directory must not be a symbolic link")
    try:
        resolved_parent = parent.resolve(strict=True)
    except OSError as exc:
        raise AdbTransferError(f"could not resolve local destination directory: {exc}") from exc
    if not resolved_parent.is_dir():
        raise AdbTransferError("local destination parent is not a directory")
    destination = resolved_parent / requested.name
    if destination.is_symlink():
        raise AdbTransferError("local destination must not be a symbolic link")
    exists = destination.exists()
    if exists and not destination.is_file():
        raise AdbTransferError("local destination exists and is not a regular file")
    if exists and not replace:
        raise AdbTransferError("local destination exists; explicit replace intent is required")
    return destination, exists


def _commit_local_stage(stage: Path, destination: Path, *, replace: bool) -> None:
    if replace:
        os.replace(stage, destination)
        return
    try:
        os.link(stage, destination)
    except FileExistsError as exc:
        raise AdbTransferError("local destination appeared during no-clobber commit") from exc
    except OSError as exc:
        raise AdbTransferError(f"local no-clobber commit failed: {exc}") from exc
    try:
        stage.unlink()
    except OSError as exc:
        raise AdbTransferError(
            f"local staging cleanup failed after no-clobber commit: {exc}",
            action_attempted=True,
            committed=True,
            cleanup_failed=True,
        ) from exc


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(_HASH_CHUNK_BYTES):
            digest.update(chunk)
    return digest.hexdigest()


def _fsync_file(path: Path) -> None:
    with path.open("rb") as handle:
        os.fsync(handle.fileno())


def _sync_directory(path: Path) -> bool:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        try:
            os.fsync(descriptor)
        except OSError as exc:
            if exc.errno in _UNSUPPORTED_DIRECTORY_FSYNC:
                return False
            raise
    finally:
        os.close(descriptor)
    return True
