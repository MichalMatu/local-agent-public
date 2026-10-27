"""Recover only a proven redundant, pre-boot control Git shallow lock.

Call under the existing control Git lock and managed checkout execution
lease. Current-boot locks, changed metadata and unavailable evidence remain
untouched. This does not recover index locks, rebases, tasks or user files.
"""
from __future__ import annotations

import os
from pathlib import Path
import re
import stat
import tempfile
from typing import Any

_MAX_BYTES = 1024 * 1024


def _identity(info: os.stat_result) -> tuple[int, ...]:
    # Reading may legitimately update atime. Identity and mutation evidence
    # must remain stable, including nanoseconds rather than rounded seconds.
    return (info.st_dev, info.st_ino, info.st_mode, info.st_uid, info.st_gid,
            info.st_nlink, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _read(path: Path) -> tuple[os.stat_result, bytes]:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        before = os.fstat(fd)
        if (not stat.S_ISREG(before.st_mode) or before.st_uid != os.getuid()
                or before.st_nlink != 1 or before.st_mode & 0o022
                or not 0 < before.st_size <= _MAX_BYTES):
            raise RuntimeError("unsafe control Git shallow metadata; inspect manually")
        with os.fdopen(fd, "rb", closefd=False) as stream:
            raw = stream.read(_MAX_BYTES + 1)
        if _identity(os.fstat(fd)) != _identity(before) or len(raw) != before.st_size:
            raise RuntimeError("control Git shallow metadata changed while reading")
        return before, raw
    finally:
        os.close(fd)


def recover_preboot_shallow_lock(core: Any) -> None:
    """Back up before unlinking an unchanged duplicate left by a prior boot."""
    control = Path(core.CONTROL)
    directory = control / ".git"
    lock = directory / "shallow.lock"
    if not lock.exists() and not lock.is_symlink():
        return
    for path in (control, directory):
        info = path.lstat()
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
                or info.st_mode & 0o022):
            raise RuntimeError("unsafe control checkout for Git lock recovery")
    before, raw = _read(lock)
    shallow = directory / "shallow"
    shallow_info, shallow_raw = _read(shallow)
    if raw != shallow_raw or re.fullmatch(rb"(?:[0-9a-f]{40}(?:[0-9a-f]{24})?\n)+", raw) is None:
        return  # Only the demonstrated redundant metadata case is recoverable.
    boot = core.process(["/usr/sbin/sysctl", "-n", "kern.boottime"], control,
                        timeout=5, log_commands=False)
    match = re.match(r"\{\s*sec = (\d+), usec = (\d+)\s*\}(?:\s|$)",
                     str(boot.get("output", "")))
    if boot["exit_code"] != 0 or boot.get("timed_out") or match is None:
        return
    boot_ns = int(match[1]) * 1_000_000_000 + int(match[2]) * 1000
    if int(match[1]) <= 0 or not 0 <= int(match[2]) < 1_000_000:
        return
    if max(before.st_mtime_ns, before.st_ctime_ns) >= boot_ns:
        return
    holders = core.process(["/usr/sbin/lsof", "-t", str(lock)], control,
                           timeout=5, log_commands=False)
    if (holders["exit_code"] != 1 or holders.get("timed_out")
            or str(holders.get("output", "")).strip()):
        return
    fd, name = tempfile.mkstemp(prefix="local-agent-preboot-shallow-",
                                suffix=".lock", dir=directory)
    with os.fdopen(fd, "wb") as backup:
        backup.write(raw)
        backup.flush()
        os.fsync(backup.fileno())
    directory_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(directory_fd)
        if (_identity(lock.lstat()) != _identity(before)
                or _identity(shallow.lstat()) != _identity(shallow_info)
                or lock.read_bytes() != raw or shallow.read_bytes() != raw):
            raise RuntimeError("control Git shallow metadata changed before recovery")
        lock.unlink()
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)
    logger = getattr(core, "log", None)
    if callable(logger):
        logger(f"recovered redundant pre-boot Git shallow lock; backup={name}")
