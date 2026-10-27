"""Linux-native bounded artifact inspection and atomic, no-clobber local copy.

All file paths must be explicit and absolute. Component-by-component O_NOFOLLOW
directory traversal prevents symlink escapes; no repository or service state
is treated specially or modified implicitly.
"""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import os
from pathlib import Path
import secrets
import stat
import time
from typing import Iterator

DEFAULT_MAX_BYTES = 512 * 1024 * 1024
HARD_MAX_BYTES = 16 * 1024 * 1024 * 1024
_CHUNK = 1024 * 1024


class ArtifactError(ValueError):
    """Rejected path, bounds or unstable source. No automatic retry."""


def _limits(max_bytes: int, timeout: float) -> float:
    if type(max_bytes) is not int or not 0 < max_bytes <= HARD_MAX_BYTES:
        raise ArtifactError("max_bytes must be a positive integer <= 16 GiB")
    if not 0 < timeout <= 300:
        raise ArtifactError("timeout must be in (0, 300] seconds")
    return time.monotonic() + timeout


def _check(deadline: float) -> None:
    if time.monotonic() >= deadline:
        raise ArtifactError("artifact operation exceeded whole-operation timeout")


@contextmanager
def _parent(path: str) -> Iterator[tuple[int, str]]:
    if not hasattr(os, "O_NOFOLLOW") or not hasattr(os, "O_DIRECTORY"):
        raise ArtifactError("no-follow directory traversal unavailable")
    p = Path(path)
    if not p.is_absolute() or ".." in p.parts or len(p.parts) < 2 or "\x00" in path:
        raise ArtifactError("artifact path must be absolute, without traversal")
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in p.parts[1:-1]:
            following = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = following
        yield fd, p.name
    finally:
        os.close(fd)


def _regular(fd: int, max_bytes: int) -> os.stat_result:
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode):
        raise ArtifactError("artifact must be a regular file")
    if info.st_size > max_bytes:
        raise ArtifactError("artifact exceeds max_bytes")
    return info


def _same(a: os.stat_result, b: os.stat_result) -> bool:
    return (
        a.st_dev, a.st_ino, a.st_size, a.st_mtime_ns, a.st_ctime_ns
    ) == (
        b.st_dev, b.st_ino, b.st_size, b.st_mtime_ns, b.st_ctime_ns
    )


def _hash(fd: int, max_bytes: int, deadline: float) -> tuple[int, str]:
    digest = hashlib.sha256()
    count = 0
    while True:
        _check(deadline)
        blob = os.read(fd, _CHUNK)
        _check(deadline)
        if not blob:
            return count, digest.hexdigest()
        count += len(blob)
        if count > max_bytes:
            raise ArtifactError("artifact grew beyond max_bytes")
        digest.update(blob)


def _source_matches(parent: int, name: str, initial: os.stat_result) -> None:
    now = os.stat(name, dir_fd=parent, follow_symlinks=False)
    if not _same(initial, now):
        raise ArtifactError("artifact changed during operation")


@contextmanager
def _open_source(path: str, max_bytes: int) -> Iterator[tuple[int, int, str, os.stat_result]]:
    with _parent(path) as (parent, name):
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        try:
            initial = _regular(fd, max_bytes)
            _source_matches(parent, name, initial)
            yield fd, parent, name, initial
        finally:
            os.close(fd)


def inspect_artifact(path: str, *, max_bytes: int = DEFAULT_MAX_BYTES,
                     timeout: float = 60.0) -> dict:
    deadline = _limits(max_bytes, timeout)
    try:
        with _open_source(path, max_bytes) as (fd, parent, name, initial):
            count, digest = _hash(fd, max_bytes, deadline)
            final = os.fstat(fd)
            _source_matches(parent, name, initial)
            if not _same(initial, final) or count != final.st_size:
                raise ArtifactError("artifact changed during inspection")
            return {
                "path": path,
                "size_bytes": count,
                "sha256": digest,
                "modified_time_ns": final.st_mtime_ns,
                "verified_stable": True,
            }
    except OSError as exc:
        raise ArtifactError(f"artifact inspection failed: {exc.strerror or type(exc).__name__}") from exc


def copy_artifact(source: str, destination: str, *, max_bytes: int = DEFAULT_MAX_BYTES,
                  timeout: float = 60.0) -> dict:
    """One explicitly named target file, atomic hard-link commit, no replacement."""
    deadline = _limits(max_bytes, timeout)
    committed = False
    temporary = ".la-hostops-" + secrets.token_hex(12) + ".part"
    try:
        with _open_source(source, max_bytes) as (src, src_parent, src_name, initial):
            with _parent(destination) as (dst_parent, dst_name):
                tempfd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                                 0o600, dir_fd=dst_parent)
                try:
                    digest = hashlib.sha256()
                    total = 0
                    try:
                        while True:
                            _check(deadline)
                            chunk = os.read(src, _CHUNK)
                            _check(deadline)
                            if not chunk:
                                break
                            total += len(chunk)
                            if total > max_bytes:
                                raise ArtifactError("artifact grew beyond max_bytes")
                            digest.update(chunk)
                            offset = 0
                            while offset < len(chunk):
                                _check(deadline)
                                written = os.write(tempfd, chunk[offset:])
                                if written <= 0:
                                    raise ArtifactError("short artifact write")
                                offset += written
                        os.fsync(tempfd)
                        _check(deadline)
                        if total != initial.st_size or not _same(initial, os.fstat(src)):
                            raise ArtifactError("source changed during copy")
                        _source_matches(src_parent, src_name, initial)
                        # No replace: link raises EEXIST if another file wins.
                        os.link(temporary, dst_name, src_dir_fd=dst_parent,
                                dst_dir_fd=dst_parent, follow_symlinks=False)
                        committed = True
                        os.fsync(dst_parent)
                        _check(deadline)
                        with os.fdopen(os.open(dst_name, os.O_RDONLY | os.O_NOFOLLOW,
                                               dir_fd=dst_parent), "rb") as verified:
                            check_size, check_digest = _hash(verified.fileno(), max_bytes, deadline)
                        if check_size != total or check_digest != digest.hexdigest():
                            raise ArtifactError("post-commit destination verification failed")
                        return {
                            "source": source, "destination": destination,
                            "size_bytes": total, "sha256": digest.hexdigest(),
                            "replaced": False, "directory_fsynced": True,
                            "verified": True,
                        }
                    finally:
                        os.close(tempfd)
                        os.unlink(temporary, dir_fd=dst_parent)
                except FileExistsError as exc:
                    raise ArtifactError("destination already exists (no-clobber)") from exc
    except ArtifactError as exc:
        if committed:
            raise ArtifactError(
                "artifact destination already committed; post-commit verification incomplete; "
                "do not replay blindly: " + str(exc)
            ) from exc
        raise
    except OSError as exc:
        stage = "after commit" if committed else "before commit"
        raise ArtifactError(f"artifact copy failed {stage}: {exc.strerror or type(exc).__name__}") from exc
