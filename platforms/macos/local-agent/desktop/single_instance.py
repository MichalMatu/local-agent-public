"""One process per desktop user for the optional GUI only.

This lock does not guard or control the Local Agent daemon and is never
imported by headless agents. It intentionally does not bypass conflicts,
remove a live lock, or create a second task/scheduler process.
"""
from __future__ import annotations

import os
import stat
from pathlib import Path


def acquire_desktop_menu_lock(*, directory: Path | None = None):
    """Return an acquired QLockFile, or None if another menu holds it.

    Fail closed for unsafe/cache-unavailable paths. Qt's indefinite-lived
    lock uses staleLockTime(0), avoiding accidental timeout of a live UI.
    The caller MUST call unlock() on normal GUI exit.
    """
    from PySide6.QtCore import QLockFile, QStandardPaths

    if directory is None:
        cache = QStandardPaths.writableLocation(
            QStandardPaths.StandardLocation.GenericCacheLocation
        )
        if not cache:
            raise RuntimeError("no user cache directory available")
        directory = Path(cache) / "local-agent-menu"
    directory = Path(directory)
    if not directory.is_absolute():
        raise ValueError("GUI lock directory must be absolute")
    try:
        os.mkdir(directory, 0o700)
    except FileExistsError:
        pass

    meta = os.lstat(directory)
    if not stat.S_ISDIR(meta.st_mode):
        raise RuntimeError("GUI lock directory is not a regular directory")
    if os.name == "posix" and (
        meta.st_uid != os.getuid() or meta.st_mode & 0o077
    ):
        raise RuntimeError("GUI lock directory must be private to its OS user")

    lock = QLockFile(str(directory / "instance.lock"))
    lock.setStaleLockTime(0)
    if lock.tryLock(0):
        return lock
    if lock.error() == QLockFile.LockError.LockFailedError:
        return None
    raise RuntimeError(f"GUI lock unavailable: {lock.error()}")
