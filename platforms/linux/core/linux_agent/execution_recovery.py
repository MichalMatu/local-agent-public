"""Linux /proc orphan detection adapted from Mac Core lease_recovery.

Called only while holding the installation's single daemon lock. Never signal
a process based merely on its PID, name or a path matching a free FD.
The process must hold the exact task-specific BSD flock in /proc/fdinfo.
"""
from __future__ import annotations

import fcntl
import os
import signal
import time
from pathlib import Path


class UnresolvedTaskLease(RuntimeError):
    """Fail closed: prior command effects may still be active."""


def lease_is_busy(path: Path) -> bool:
    if not path.exists():
        return False
    with path.open("r", encoding="utf-8") as stream:
        try:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
    return False


def lease_holders(path: Path) -> set[int]:
    """Same Linux fdinfo/FLOCK ownership check as Mac foundation.lease_recovery."""
    try:
        stat = path.stat()
    except FileNotFoundError:
        return set()
    target = (stat.st_dev, stat.st_ino)
    holders: set[int] = set()
    for process_dir in Path("/proc").iterdir():
        if not process_dir.name.isdecimal():
            continue
        try:
            descriptors = tuple((process_dir / "fd").iterdir())
        except (FileNotFoundError, NotADirectoryError, PermissionError, OSError):
            continue
        for descriptor in descriptors:
            try:
                info = descriptor.stat()
                if (info.st_dev, info.st_ino) != target:
                    continue
                fdinfo = (process_dir / "fdinfo" / descriptor.name).read_text(
                    encoding="utf-8"
                )
                if any(line.startswith("lock:") and " FLOCK " in line
                       for line in fdinfo.splitlines()):
                    holders.add(int(process_dir.name))
                    break
            except (FileNotFoundError, PermissionError, OSError):
                continue
    return holders


def _signal_holder(pid: int, signo: int) -> None:
    """pidfds prevent PID reuse races during targeted signal delivery."""
    if not hasattr(os, "pidfd_open") or not hasattr(signal, "pidfd_send_signal"):
        raise UnresolvedTaskLease("kernel/runtime does not support pidfd signaling")
    try:
        fd = os.pidfd_open(pid, 0)
    except ProcessLookupError:
        return
    try:
        try:
            signal.pidfd_send_signal(fd, signo)
        except ProcessLookupError:
            return
    finally:
        os.close(fd)


def recover_task_lease(path: Path) -> tuple[int, ...]:
    """Stop orphan holders of a precise task lease before UNKNOWN publication.

    Caller must hold daemon_lock and must not have a live worker. A PID is a
    target only while its fdinfo reports an inherited lock for this inode.
    Unknown/ambiguous holders produce a hard error, not automatic replay.
    """
    if not lease_is_busy(path):
        return ()
    protected = {os.getpid(), os.getppid(), 0, 1}
    victims = tuple(sorted(lease_holders(path) - protected))
    if not victims:
        raise UnresolvedTaskLease("task execution lock busy but holder not verified")
    for pid in victims:
        if pid in lease_holders(path):
            _signal_holder(pid, signal.SIGTERM)
    deadline = time.monotonic() + 1.5
    while time.monotonic() < deadline:
        if not lease_is_busy(path):
            return victims
        time.sleep(0.05)
    for pid in victims:
        if pid in lease_holders(path):
            _signal_holder(pid, signal.SIGKILL)
    deadline = time.monotonic() + 1.5
    while time.monotonic() < deadline:
        if not lease_is_busy(path):
            return victims
        time.sleep(0.05)
    raise UnresolvedTaskLease("task execution lock still held after targeted cleanup")
