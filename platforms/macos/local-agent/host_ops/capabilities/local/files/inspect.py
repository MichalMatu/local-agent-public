"""Read-only local artifact inspection with digest evidence."""

from __future__ import annotations

import hashlib
import os
import stat
from pathlib import Path

from local_agent.host_ops.core.execution import ExecutionLimits

from .bounds import (
    DEFAULT_MAX_ARTIFACT_BYTES,
    ArtifactOperationBudget,
    ArtifactOperationTimeout,
    validate_max_artifact_bytes,
)
from .models import ArtifactInspectionResult

_HASH_CHUNK_BYTES = 1024 * 1024


class ArtifactInspectionError(ValueError):
    """Raised when a requested artifact cannot be inspected safely."""


class LocalArtifactInspector:
    def inspect(
        self,
        source: Path,
        *,
        max_bytes: int = DEFAULT_MAX_ARTIFACT_BYTES,
        limits: ExecutionLimits | None = None,
    ) -> ArtifactInspectionResult:
        try:
            maximum = validate_max_artifact_bytes(max_bytes)
        except ValueError as exc:
            raise ArtifactInspectionError(str(exc)) from exc
        budget = ArtifactOperationBudget(limits)
        requested = source.expanduser()

        try:
            budget.checkpoint()
            if not hasattr(os, "O_NOFOLLOW"):
                raise ArtifactInspectionError("host does not support no-follow artifact inspection")

            try:
                initial = requested.lstat()
            except OSError as exc:
                raise ArtifactInspectionError(f"could not inspect artifact {requested}: {exc}") from exc
            budget.checkpoint()
            if stat.S_ISLNK(initial.st_mode):
                raise ArtifactInspectionError("artifact must not be a symbolic link")
            if not stat.S_ISREG(initial.st_mode):
                raise ArtifactInspectionError("artifact is not a regular file")
            if initial.st_size > maximum:
                raise ArtifactInspectionError(
                    f"artifact exceeds max_bytes ({initial.st_size} > {maximum})"
                )

            descriptor = -1
            try:
                descriptor = os.open(requested, os.O_RDONLY | os.O_NOFOLLOW)
                budget.checkpoint()
                opened_before = os.fstat(descriptor)
                if not stat.S_ISREG(opened_before.st_mode):
                    raise ArtifactInspectionError("artifact is not a regular file")
                if _identity(opened_before) != _identity(initial):
                    raise ArtifactInspectionError("artifact changed while being opened")
                if opened_before.st_size > maximum:
                    raise ArtifactInspectionError(
                        f"artifact exceeds max_bytes ({opened_before.st_size} > {maximum})"
                    )
                digest = _sha256_fd(descriptor, maximum, budget)
                opened_after = os.fstat(descriptor)
                budget.checkpoint()
            except ArtifactInspectionError:
                raise
            except OSError as exc:
                raise ArtifactInspectionError(f"could not read artifact {requested}: {exc}") from exc
            finally:
                if descriptor >= 0:
                    os.close(descriptor)

            if _stable_file_state(opened_before) != _stable_file_state(opened_after):
                raise ArtifactInspectionError("artifact changed while being inspected")

            try:
                current = requested.lstat()
                real_path = requested.resolve(strict=True)
                real_metadata = real_path.stat()
            except OSError as exc:
                raise ArtifactInspectionError(f"could not verify artifact {requested}: {exc}") from exc
            budget.checkpoint()
            if stat.S_ISLNK(current.st_mode):
                raise ArtifactInspectionError("artifact became a symbolic link while being inspected")
            if not stat.S_ISREG(current.st_mode) or not stat.S_ISREG(real_metadata.st_mode):
                raise ArtifactInspectionError("artifact changed type while being inspected")
            if _identity(current) != _identity(opened_after) or _identity(real_metadata) != _identity(
                opened_after
            ):
                raise ArtifactInspectionError("artifact path changed while being inspected")
            if _stable_file_state(real_metadata) != _stable_file_state(opened_after):
                raise ArtifactInspectionError("artifact changed while being inspected")

            return ArtifactInspectionResult(
                requested_path=requested.absolute(),
                real_path=real_path,
                file_type="regular_file",
                size_bytes=opened_after.st_size,
                sha256=digest,
                modified_time_ns=opened_after.st_mtime_ns,
            )
        except ArtifactOperationTimeout as exc:
            raise ArtifactInspectionError(
                "artifact inspection exceeded its whole-operation timeout"
            ) from exc


def _identity(metadata: os.stat_result) -> tuple[int, int]:
    return metadata.st_dev, metadata.st_ino


def _stable_file_state(metadata: os.stat_result) -> tuple[int, int, int]:
    return metadata.st_size, metadata.st_mtime_ns, metadata.st_ctime_ns


def _sha256_fd(
    descriptor: int,
    max_bytes: int,
    budget: ArtifactOperationBudget,
) -> str:
    digest = hashlib.sha256()
    size_bytes = 0
    while True:
        budget.checkpoint()
        chunk = os.read(descriptor, _HASH_CHUNK_BYTES)
        budget.checkpoint()
        if not chunk:
            break
        size_bytes += len(chunk)
        if size_bytes > max_bytes:
            raise ArtifactInspectionError(
                f"artifact exceeds max_bytes ({size_bytes} > {max_bytes})"
            )
        digest.update(chunk)
    return digest.hexdigest()
