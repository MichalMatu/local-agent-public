"""Bounded local hashing and immutable upload staging.

These helpers own no transport, daemon state, scheduling or process lifecycle.
"""

from __future__ import annotations

import errno
import hashlib
import os
import tempfile
from pathlib import Path
from typing import Protocol

_HASH_CHUNK_BYTES = 1024 * 1024


class BudgetCheck(Protocol):
    def check(self) -> None:
        ...


class LocalTransferBoundsError(ValueError):
    """Local data changed or grew beyond the declared transfer bound."""


class LocalTransferSizeLimitError(LocalTransferBoundsError):
    """A local source exceeded its byte ceiling."""


class LocalTransferSourceChangedError(LocalTransferBoundsError):
    """A source no longer matches the inspected immutable content."""


def sha256_file(
    path: Path, *, budget: BudgetCheck | None = None,
    max_bytes: int | None = None,
) -> str:
    digest = hashlib.sha256()
    bytes_read = 0
    if budget is not None:
        budget.check()
    with path.open("rb") as handle:
        while True:
            if budget is not None:
                budget.check()
            read_limit = _HASH_CHUNK_BYTES
            if max_bytes is not None:
                read_limit = min(read_limit, max_bytes - bytes_read + 1)
            chunk = handle.read(read_limit)
            if budget is not None:
                budget.check()
            if not chunk:
                break
            bytes_read += len(chunk)
            if max_bytes is not None and bytes_read > max_bytes:
                raise LocalTransferSizeLimitError(
                    f"local source exceeds max_bytes during hashing ({bytes_read} > {max_bytes})"
                )
            digest.update(chunk)
    if budget is not None:
        budget.check()
    return digest.hexdigest()


def bounded_upload_snapshot(
    source: Path,
    expected_size: int,
    expected_sha256: str,
    max_bytes: int,
    budget: BudgetCheck,
) -> Path:
    """Freeze bounded source bytes in an exclusively created private file."""
    budget.check()
    descriptor, filename = tempfile.mkstemp(prefix="hostops-upload-", suffix=".tmp")
    snapshot = Path(filename)
    try:
        digest = hashlib.sha256()
        copied = 0
        with os.fdopen(descriptor, "wb") as output, source.open("rb") as original:
            while True:
                budget.check()
                chunk = original.read(min(_HASH_CHUNK_BYTES, max_bytes - copied + 1))
                budget.check()
                if not chunk:
                    break
                copied += len(chunk)
                if copied > max_bytes:
                    raise LocalTransferSizeLimitError(
                        f"local source exceeds max_bytes during snapshot ({copied} > {max_bytes})"
                    )
                output.write(chunk)
                digest.update(chunk)
            output.flush()
            budget.check()
        if copied != expected_size or digest.hexdigest() != expected_sha256:
            raise LocalTransferSourceChangedError(
                "local source changed while preparing upload snapshot"
            )
        budget.check()
        return snapshot
    except BaseException as exc:
        try:
            snapshot.unlink(missing_ok=True)
        except OSError as cleanup:
            exc.add_note(f"private upload snapshot cleanup also failed: {cleanup}")
        raise


_UNSUPPORTED_DIRECTORY_FSYNC = {errno.EINVAL, errno.EBADF}
if hasattr(errno, "ENOTSUP"):
    _UNSUPPORTED_DIRECTORY_FSYNC.add(errno.ENOTSUP)


def fsync_file(path: Path) -> None:
    with path.open("rb") as handle:
        os.fsync(handle.fileno())


def sync_directory(path: Path) -> bool:
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
