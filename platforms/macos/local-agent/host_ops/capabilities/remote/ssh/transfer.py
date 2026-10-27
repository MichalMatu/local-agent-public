"""Bounded verified file transfer over the configured OpenSSH transport."""

from __future__ import annotations

import os
import secrets
import stat
import time
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from local_agent.host_ops.core.config import HostTarget
from local_agent.host_ops.core.execution import (
    ExecutionLimits,
    ProcessResult,
    ProcessRunner,
    ProcessState,
)

from .client import SshClient
from .command import SshOptions, build_scp_push_command
from .errors import SshTransferError, _conflict_error, _input_error, _integrity_error
from .local_stage import (
    LocalTransferBoundsError,
    LocalTransferSizeLimitError,
    bounded_upload_snapshot,
    fsync_file as _fsync_file,
    sha256_file as _sha256_file,
    sync_directory as _sync_directory,
)
from .remote_commit import _commit_remote_no_clobber, _commit_remote_replace
from .remote_probes import _RemoteProbes, _process_failure, _require_process_ok
from .paths import normalize_remote_file_path

DEFAULT_MAX_TRANSFER_BYTES = 512 * 1024 * 1024
MAX_TRANSFER_BYTES = 16 * 1024 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class SshTransferResult:
    direction: str
    target: str
    source: str
    destination: str
    size_bytes: int
    sha256: str
    replaced_existing: bool
    staging_cleaned: bool = True
    local_directory_synced: bool | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "direction": self.direction,
            "target": self.target,
            "source": self.source,
            "destination": self.destination,
            "size_bytes": self.size_bytes,
            "sha256": self.sha256,
            "replaced_existing": self.replaced_existing,
            "staging_cleaned": self.staging_cleaned,
            "local_directory_synced": self.local_directory_synced,
        }


class SshFileTransfer(_RemoteProbes):
    def __init__(
        self,
        runner: ProcessRunner | None = None,
        *,
        options: SshOptions | None = None,
    ) -> None:
        self._runner = runner or ProcessRunner()
        self._options = options or SshOptions()
        self._client = SshClient(self._runner, options=self._options)

    def push(
        self,
        target: HostTarget,
        local_source: Path,
        remote_destination: str,
        *,
        replace: bool = False,
        max_bytes: int = DEFAULT_MAX_TRANSFER_BYTES,
        limits: ExecutionLimits | None = None,
    ) -> SshTransferResult:
        maximum = _validate_max_bytes(max_bytes)
        budget = _TransferBudget(limits or ExecutionLimits(timeout_seconds=300.0))
        source = _inspect_local_source(local_source, maximum, budget=budget)
        destination = _validated_remote_path(remote_destination)
        parent = str(PurePosixPath(destination).parent)
        self._require_remote_directory(target, parent, budget)
        replaced_existing = self._validate_remote_destination(
            target,
            destination,
            replace=replace,
            budget=budget,
        )
        stage = str(PurePosixPath(parent) / f".hostops-upload-{secrets.token_hex(12)}.tmp")
        stage_present = False
        stage_may_exist = False
        action_attempted = False
        committed = False
        snapshot: Path | None = None

        try:
            snapshot = _bounded_upload_snapshot(source, maximum, budget)
            command = build_scp_push_command(target, snapshot, stage, options=self._options)
            scp_limits = budget.remaining()
            action_attempted = True
            stage_may_exist = True
            upload_process = self._runner.run(command, limits=scp_limits)
            if upload_process.state is ProcessState.SPAWN_FAILED:
                action_attempted = False
                stage_may_exist = False
            _require_process_ok(upload_process, "SCP upload")
            stage_present = True
            remote_size = self._remote_size(target, stage, budget)
            remote_sha256 = self._remote_sha256(target, stage, budget)
            if remote_size != source.size_bytes or remote_sha256 != source.sha256:
                raise _integrity_error("uploaded staging file failed size or SHA-256 verification")
            if _sha256_file(source.path, budget=budget, max_bytes=maximum) != source.sha256:
                raise _integrity_error("local source changed while upload was in progress")

            budget.check()
            if replace:
                _commit_remote_replace(self._client, target, stage, destination, budget)
                stage_present = False
            else:
                _commit_remote_no_clobber(self._client, target, stage, destination, budget)
                stage_present = True
            committed = True

            final_size = self._remote_size(target, destination, budget)
            final_sha256 = self._remote_sha256(target, destination, budget)
            if final_size != source.size_bytes or final_sha256 != source.sha256:
                raise _integrity_error("remote destination failed post-commit verification")

            staging_cleaned = True
            if stage_present:
                staging_cleaned = _cleanup_remote_stage(self._client, target, stage, budget)
                stage_present = not staging_cleaned
            budget.check()
            snapshot.unlink()
            snapshot = None
            budget.check()
            return SshTransferResult(
                direction="push",
                target=target.alias,
                source=str(source.path),
                destination=destination,
                size_bytes=source.size_bytes,
                sha256=source.sha256,
                replaced_existing=replaced_existing,
                staging_cleaned=staging_cleaned,
            )
        except (OSError, SshTransferError, ValueError) as exc:
            cleanup_failed = False
            if stage_present or (stage_may_exist and not committed):
                try:
                    cleanup_failed = not _cleanup_remote_stage(
                        self._client,
                        target,
                        stage,
                        budget,
                        best_effort=True,
                    )
                except (OSError, SshTransferError, ValueError):
                    cleanup_failed = True
            if snapshot is not None:
                try:
                    snapshot.unlink(missing_ok=True)
                except OSError:
                    cleanup_failed = True
            raise _ssh_transfer_error(
                exc,
                action_attempted=action_attempted,
                committed=committed,
                cleanup_failed=cleanup_failed,
            ) from exc

    def pull(
        self,
        target: HostTarget,
        remote_source: str,
        local_destination: Path,
        *,
        replace: bool = False,
        max_bytes: int = DEFAULT_MAX_TRANSFER_BYTES,
        limits: ExecutionLimits | None = None,
    ) -> SshTransferResult:
        maximum = _validate_max_bytes(max_bytes)
        budget = _TransferBudget(limits or ExecutionLimits(timeout_seconds=300.0))
        source = _validated_remote_path(remote_source)
        self._require_remote_regular_file(target, source, budget)
        remote_size = self._remote_size(target, source, budget)
        if remote_size > maximum:
            raise SshTransferError(
                f"remote source exceeds max_bytes ({remote_size} > {maximum})",
                code="ssh_size_limit_exceeded", phase="preflight",
            )
        remote_sha256 = self._remote_sha256(target, source, budget)
        destination, replaced_existing = _prepare_local_destination(
            local_destination, replace=replace
        )
        stage = destination.parent / f".hostops-download-{secrets.token_hex(12)}.tmp"
        action_attempted = False
        committed = False
        stage_owned = False
        failure: OSError | SshTransferError | ValueError | None = None
        result: SshTransferResult | None = None

        try:
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            descriptor = os.open(stage, flags, 0o600)
            stage_owned = True
            try:
                # The private stage is capped while bytes are flowing; it is
                # never an unbounded SCP-created file. SSH stdout stays binary.
                stream_limits = budget.remaining()
                action_attempted = True
                process = self._client.execute_binary_to_fd(
                    target,
                    ("cat", source),
                    sink_fd=descriptor,
                    max_bytes=maximum,
                    limits=stream_limits,
                )
                if process.state is ProcessState.SPAWN_FAILED:
                    action_attempted = False
            finally:
                os.close(descriptor)
            _require_process_ok(process, "SSH binary download")
            staged = _inspect_local_source(stage, maximum, budget=budget)
            if staged.size_bytes != remote_size or staged.sha256 != remote_sha256:
                raise _integrity_error(
                    "downloaded staging file failed size or SHA-256 verification"
                )
            budget.check()
            _fsync_file(stage)
            budget.check()
            if replace:
                os.replace(stage, destination)
                committed = True
            else:
                try:
                    os.link(stage, destination)
                except FileExistsError as exc:
                    raise _conflict_error(
                        "local destination appeared during no-clobber commit"
                    ) from exc
                except OSError as exc:
                    raise SshTransferError(f"local no-clobber commit failed: {exc}") from exc
                committed = True
                try:
                    stage.unlink()
                except OSError as exc:
                    raise SshTransferError(
                        f"local staging cleanup failed after no-clobber commit: {exc}",
                        action_attempted=True,
                        committed=True,
                        cleanup_failed=True,
                    ) from exc
            directory_synced = _sync_directory(destination.parent)
            budget.check()
            result = SshTransferResult(
                direction="pull",
                target=target.alias,
                source=source,
                destination=str(destination),
                size_bytes=remote_size,
                sha256=remote_sha256,
                replaced_existing=replaced_existing,
                local_directory_synced=directory_synced,
            )
        except (OSError, SshTransferError, ValueError) as exc:
            failure = exc

        cleanup_failed = False
        # Never clean up an O_EXCL collision that we did not create.
        if stage_owned and (stage.exists() or stage.is_symlink()):
            try:
                stage.unlink(missing_ok=True)
            except OSError:
                cleanup_failed = True

        if failure is not None:
            raise _ssh_transfer_error(
                failure,
                action_attempted=action_attempted,
                committed=committed,
                cleanup_failed=cleanup_failed,
            ) from failure
        if cleanup_failed:
            raise SshTransferError(
                "local staging cleanup failed after SSH pull",
                action_attempted=action_attempted,
                committed=committed,
                cleanup_failed=True,
            )
        assert result is not None
        try:
            budget.check()
        except SshTransferError as exc:
            raise _ssh_transfer_error(
                exc,
                action_attempted=action_attempted,
                committed=committed,
                cleanup_failed=False,
            ) from exc
        return result



def _cleanup_remote_stage(
    client: SshClient,
    target: HostTarget,
    stage: str,
    budget: _TransferBudget,
    *,
    best_effort: bool = False,
) -> bool:
    try:
        result = client.execute(target, ("rm", "-f", stage), limits=budget.remaining())
    except SshTransferError:
        if best_effort:
            return False
        raise
    if result.ok:
        return True
    if best_effort:
        return False
    raise SshTransferError(
        _process_failure("remote staging cleanup", result),
        code="ssh_cleanup_failed", phase="cleanup", process=result,
    )


def _ssh_transfer_error(
    exc: OSError | SshTransferError | ValueError,
    *,
    action_attempted: bool,
    committed: bool,
    cleanup_failed: bool,
) -> SshTransferError:
    code = "ssh_transfer_failed"
    phase: str | None = None
    process: ProcessResult | None = None
    commit_uncertain = False
    if isinstance(exc, SshTransferError):
        action_attempted = action_attempted or exc.action_attempted
        committed = committed or exc.committed
        cleanup_failed = cleanup_failed or exc.cleanup_failed
        code = exc.code
        phase = exc.phase
        process = exc.process
        commit_uncertain = exc.commit_uncertain
    elif isinstance(exc, LocalTransferSizeLimitError):
        code, phase = "ssh_size_limit_exceeded", "local_source"
    elif isinstance(exc, LocalTransferBoundsError):
        code, phase = "ssh_integrity_mismatch", "local_source"
    elif isinstance(exc, OSError):
        code, phase = "ssh_local_io_failed", "filesystem"
    elif isinstance(exc, ValueError):
        code, phase = "ssh_invalid_input", "preflight"
    return SshTransferError(
        str(exc),
        action_attempted=action_attempted or committed,
        committed=committed,
        cleanup_failed=cleanup_failed,
        commit_uncertain=commit_uncertain,
        code=code,
        phase=phase,
        process=process,
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

    def check(self) -> None:
        self.remaining()

    def remaining(self) -> ExecutionLimits:
        remaining = self._deadline - time.monotonic()
        if remaining <= 0:
            raise SshTransferError(
                "file transfer exceeded its whole-operation timeout",
                code="ssh_timeout", phase="budget",
            )
        return ExecutionLimits(
            timeout_seconds=remaining,
            terminate_grace_seconds=self._limits.terminate_grace_seconds,
            pipe_drain_seconds=self._limits.pipe_drain_seconds,
            max_stdout_bytes=self._limits.max_stdout_bytes,
            max_stderr_bytes=self._limits.max_stderr_bytes,
        )


def _inspect_local_source(
    path: Path, max_bytes: int, *, budget: _TransferBudget | None = None,
) -> _LocalSource:
    if budget is not None:
        budget.check()
    source = Path(path).expanduser()
    if source.is_symlink():
        raise _input_error("local source must not be a symbolic link")
    try:
        resolved = source.resolve(strict=True)
        metadata = resolved.stat()
    except OSError as exc:
        raise SshTransferError(f"could not inspect local source: {exc}") from exc
    if budget is not None:
        budget.check()
    if not stat.S_ISREG(metadata.st_mode):
        raise _input_error("local source is not a regular file")
    if metadata.st_size > max_bytes:
        raise SshTransferError(
            f"local source exceeds max_bytes ({metadata.st_size} > {max_bytes})",
            code="ssh_size_limit_exceeded", phase="local_source",
        )
    try:
        digest = _sha256_file(resolved, budget=budget, max_bytes=max_bytes)
    except LocalTransferBoundsError as exc:
        raise SshTransferError(
            str(exc),
            code=("ssh_size_limit_exceeded" if isinstance(exc, LocalTransferSizeLimitError)
                  else "ssh_integrity_mismatch"),
            phase="local_source",
        ) from exc
    except OSError as exc:
        raise SshTransferError(f"could not hash local source: {exc}") from exc
    if budget is not None:
        budget.check()
    return _LocalSource(resolved, metadata.st_size, digest)

def _bounded_upload_snapshot(
    source: _LocalSource, max_bytes: int, budget: _TransferBudget,
) -> Path:
    try:
        return bounded_upload_snapshot(
            source.path, source.size_bytes, source.sha256, max_bytes, budget,
        )
    except LocalTransferBoundsError as exc:
        raise SshTransferError(
            str(exc),
            code=("ssh_size_limit_exceeded" if isinstance(exc, LocalTransferSizeLimitError)
                  else "ssh_integrity_mismatch"),
            phase="local_snapshot",
        ) from exc


def _prepare_local_destination(path: Path, *, replace: bool) -> tuple[Path, bool]:
    requested = Path(path).expanduser()
    parent = requested.parent
    if parent.is_symlink():
        raise _input_error("local destination directory must not be a symbolic link")
    try:
        resolved_parent = parent.resolve(strict=True)
    except OSError as exc:
        raise SshTransferError(f"could not resolve local destination directory: {exc}") from exc
    if not resolved_parent.is_dir():
        raise SshTransferError("local destination parent is not a directory")
    destination = resolved_parent / requested.name
    if destination.is_symlink():
        raise _conflict_error("local destination must not be a symbolic link")
    exists = destination.exists()
    if exists and not destination.is_file():
        raise _conflict_error("local destination exists and is not a regular file")
    if exists and not replace:
        raise _conflict_error("local destination exists; explicit replace intent is required")
    return destination, exists


def _validated_remote_path(path: str) -> str:
    try:
        return normalize_remote_file_path(path)
    except ValueError as exc:
        raise _input_error(str(exc)) from exc


def _validate_max_bytes(value: int) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 1 <= value <= MAX_TRANSFER_BYTES
    ):
        raise _input_error(f"max_bytes must be an integer in range 1..{MAX_TRANSFER_BYTES}")
    return value
