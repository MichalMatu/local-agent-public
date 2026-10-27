"""Copy one local artifact with explicit identity, durability steps and digest verification."""

from __future__ import annotations

import errno
import hashlib
import os
import tempfile
from contextlib import suppress
from pathlib import Path

from local_agent.host_ops.core.execution import ExecutionLimits

from .bounds import (
    DEFAULT_MAX_ARTIFACT_BYTES,
    ArtifactOperationBudget,
    ArtifactOperationTimeout,
    validate_max_artifact_bytes,
)
from .models import ArtifactDeploymentResult

_CHUNK_BYTES = 1024 * 1024
_UNSUPPORTED_DIRECTORY_FSYNC = {errno.EINVAL, errno.EBADF}
if hasattr(errno, "ENOTSUP"):
    _UNSUPPORTED_DIRECTORY_FSYNC.add(errno.ENOTSUP)


class ArtifactDeploymentError(RuntimeError):
    """Raised when artifact deployment cannot complete with trustworthy evidence."""

    def __init__(self, message: str, *, committed: bool = False) -> None:
        super().__init__(message)
        self.committed = committed


class LocalArtifactDeployer:
    """Deploy one regular file into an existing local directory and verify SHA-256."""

    def deploy(
        self,
        source: Path,
        destination_directory: Path,
        *,
        destination_name: str | None = None,
        replace: bool = False,
        max_bytes: int = DEFAULT_MAX_ARTIFACT_BYTES,
        limits: ExecutionLimits | None = None,
    ) -> ArtifactDeploymentResult:
        try:
            maximum = validate_max_artifact_bytes(max_bytes)
        except ValueError as exc:
            raise ArtifactDeploymentError(str(exc)) from exc
        budget = ArtifactOperationBudget(limits)
        committed = False
        temporary_path: Path | None = None

        try:
            budget.checkpoint()
            source_path = _regular_source(source, maximum)
            target_directory = _target_directory(destination_directory)
            budget.checkpoint()
            target_name = _destination_name(
                source_path.name if destination_name is None else destination_name
            )
            destination = target_directory / target_name

            if destination.is_symlink():
                raise ArtifactDeploymentError("destination must not be a symbolic link")
            replaced_existing = destination.exists()
            if replaced_existing and not replace:
                raise ArtifactDeploymentError(
                    f"destination already exists: {destination}; use explicit replace intent"
                )
            if replaced_existing and not destination.is_file():
                raise ArtifactDeploymentError(f"destination is not a regular file: {destination}")

            source_digest = hashlib.sha256()
            size_bytes = 0
            descriptor, temporary_name = tempfile.mkstemp(
                prefix=f".{target_name}.hostops-",
                dir=target_directory,
            )
            temporary_path = Path(temporary_name)
            with source_path.open("rb") as source_file, os.fdopen(descriptor, "wb") as target_file:
                while True:
                    budget.checkpoint()
                    chunk = source_file.read(_CHUNK_BYTES)
                    budget.checkpoint()
                    if not chunk:
                        break
                    size_bytes += len(chunk)
                    if size_bytes > maximum:
                        raise ArtifactDeploymentError(
                            f"source exceeds max_bytes ({size_bytes} > {maximum})"
                        )
                    source_digest.update(chunk)
                    target_file.write(chunk)
                    budget.checkpoint()
                target_file.flush()
                os.fsync(target_file.fileno())
                budget.checkpoint()

            if destination.is_symlink():
                raise ArtifactDeploymentError(
                    "destination became a symbolic link during deployment"
                )
            budget.checkpoint()
            if replace:
                if destination.exists() and not destination.is_file():
                    raise ArtifactDeploymentError(
                        "destination changed to a non-regular file during deployment"
                    )
                os.replace(temporary_path, destination)
            else:
                _commit_no_clobber(temporary_path, destination)
            committed = True
            temporary_path = None
            budget.checkpoint()
            directory_synced = _sync_directory(target_directory, budget)

            destination_digest, destination_size = _sha256_and_size(
                destination,
                maximum,
                budget,
            )
            expected_digest = source_digest.hexdigest()
            if destination_size != size_bytes or destination_digest != expected_digest:
                raise ArtifactDeploymentError(
                    "destination verification failed after commit; "
                    "destination may require operator cleanup"
                )
            return ArtifactDeploymentResult(
                source=source_path,
                destination=destination,
                size_bytes=size_bytes,
                sha256=expected_digest,
                replaced_existing=replaced_existing,
                directory_synced=directory_synced,
            )
        except ArtifactOperationTimeout as exc:
            state = "after commit" if committed else "before commit"
            suffix = "; destination may require operator cleanup" if committed else ""
            raise ArtifactDeploymentError(
                f"artifact deployment exceeded its whole-operation timeout {state}{suffix}",
                committed=committed,
            ) from exc
        except ArtifactDeploymentError as exc:
            if committed and not exc.committed:
                raise ArtifactDeploymentError(str(exc), committed=True) from exc
            raise
        except OSError as exc:
            state = "after commit" if committed else "before commit"
            raise ArtifactDeploymentError(
                f"artifact deployment failed {state}: {exc}",
                committed=committed,
            ) from exc
        finally:
            if temporary_path is not None:
                with suppress(OSError):
                    temporary_path.unlink(missing_ok=True)


def _commit_no_clobber(temporary_path: Path, destination: Path) -> None:
    """Reserve an absent destination before replacing only our own reservation."""
    reservation_fd = -1
    reservation_identity: tuple[int, int] | None = None
    try:
        try:
            reservation_fd = os.open(
                destination,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
            )
        except FileExistsError as exc:
            raise ArtifactDeploymentError(
                "destination appeared during deployment; refusing overwrite"
            ) from exc
        reserved = os.fstat(reservation_fd)
        reservation_identity = (reserved.st_dev, reserved.st_ino)
        current = destination.lstat()
        if (current.st_dev, current.st_ino) != reservation_identity:
            raise ArtifactDeploymentError(
                "destination reservation changed during deployment; refusing overwrite"
            )
        os.replace(temporary_path, destination)
    finally:
        if reservation_fd >= 0:
            os.close(reservation_fd)
        if reservation_identity is not None and temporary_path.exists():
            with suppress(OSError):
                current = destination.lstat()
                if (current.st_dev, current.st_ino) == reservation_identity:
                    destination.unlink()


def _regular_source(path: Path, max_bytes: int) -> Path:
    expanded = path.expanduser()
    if expanded.is_symlink():
        raise ArtifactDeploymentError("source must not be a symbolic link")
    try:
        resolved = expanded.resolve(strict=True)
        metadata = resolved.stat()
    except OSError as exc:
        raise ArtifactDeploymentError(f"source cannot be resolved: {expanded}") from exc
    if not resolved.is_file():
        raise ArtifactDeploymentError(f"source is not a regular file: {resolved}")
    if metadata.st_size > max_bytes:
        raise ArtifactDeploymentError(
            f"source exceeds max_bytes ({metadata.st_size} > {max_bytes})"
        )
    return resolved


def _target_directory(path: Path) -> Path:
    expanded = path.expanduser()
    if expanded.is_symlink():
        raise ArtifactDeploymentError("destination directory must not be a symbolic link")
    try:
        resolved = expanded.resolve(strict=True)
    except OSError as exc:
        raise ArtifactDeploymentError(
            f"destination directory cannot be resolved: {expanded}"
        ) from exc
    if not resolved.is_dir():
        raise ArtifactDeploymentError(f"destination directory is not a directory: {resolved}")
    return resolved


def _destination_name(value: str) -> str:
    if not value or value in {".", ".."} or "/" in value or "\\" in value or "\x00" in value:
        raise ArtifactDeploymentError("destination_name must be one plain filename")
    return value


def _sha256_and_size(
    path: Path,
    max_bytes: int,
    budget: ArtifactOperationBudget,
) -> tuple[str, int]:
    digest = hashlib.sha256()
    size_bytes = 0
    with path.open("rb") as handle:
        while True:
            budget.checkpoint()
            chunk = handle.read(_CHUNK_BYTES)
            budget.checkpoint()
            if not chunk:
                break
            size_bytes += len(chunk)
            if size_bytes > max_bytes:
                raise ArtifactDeploymentError(
                    f"destination exceeds max_bytes ({size_bytes} > {max_bytes}); "
                    "destination may require operator cleanup"
                )
            digest.update(chunk)
    return digest.hexdigest(), size_bytes


def _sync_directory(path: Path, budget: ArtifactOperationBudget) -> bool:
    budget.checkpoint()
    descriptor = os.open(path, os.O_RDONLY)
    try:
        try:
            os.fsync(descriptor)
        except OSError as exc:
            if exc.errno in _UNSUPPORTED_DIRECTORY_FSYNC:
                budget.checkpoint()
                return False
            raise
    finally:
        os.close(descriptor)
    budget.checkpoint()
    return True
