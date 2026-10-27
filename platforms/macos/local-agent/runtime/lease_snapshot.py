"""Read-only bounded inventory of on-disk repository lease records.

An on-disk lock file is *not* evidence that its advisory lock is held.
Never acquire, probe, rewrite or recover locks from this diagnostic.
"""
from __future__ import annotations

import os
import re
import stat
from pathlib import Path

from local_agent.runtime.health_snapshot import REGISTRY_BYTES, _private_json, _repository_ids

_MAX_LOCK_FILES = 1024
_LOCK_NAME = re.compile(r"^[0-9a-f]{64}\.lock$")
_DIR_FLAGS = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)


def _safe_child(parent_fd: int, name: str) -> int:
    fd = os.open(name, _DIR_FLAGS, dir_fd=parent_fd)
    try:
        info = os.fstat(fd)
        if not stat.S_ISDIR(info.st_mode) or (
            os.name == "posix"
            and (info.st_uid != os.getuid() or info.st_mode & 0o022)
        ):
            raise ValueError("unsafe lease inventory directory")
    except BaseException:
        os.close(fd)
        raise
    return fd


def _count_disk_lease_files(root: Path, *, no_projects: bool) -> int:
    if not root.is_absolute() or root.is_symlink():
        raise ValueError("invalid state directory")
    top = os.open(root, _DIR_FLAGS)
    try:
        state_info = os.fstat(top)
        if not stat.S_ISDIR(state_info.st_mode) or (
            os.name == "posix"
            and (state_info.st_uid != os.getuid() or state_info.st_mode & 0o022)
        ):
            raise ValueError("unsafe state directory")
        try:
            locks = _safe_child(top, "locks")
        except FileNotFoundError:
            if no_projects:
                return 0
            raise
        try:
            try:
                repository_locks = _safe_child(locks, "repositories")
            except FileNotFoundError:
                if no_projects:
                    return 0
                raise
            try:
                count = 0
                with os.scandir(repository_locks) as items:
                    for item in items:
                        count += 1
                        if count > _MAX_LOCK_FILES or _LOCK_NAME.fullmatch(item.name) is None:
                            raise ValueError("invalid or oversized lock inventory")
                        info = item.stat(follow_symlinks=False)
                        if not stat.S_ISREG(info.st_mode) or (
                            os.name == "posix"
                            and (info.st_uid != os.getuid() or info.st_mode & 0o022)
                        ):
                            raise ValueError("unsafe lease record")
                return count
            finally:
                os.close(repository_locks)
        finally:
            os.close(locks)
    finally:
        os.close(top)


def observe_local_lease_inventory(
    *, state_root: Path, registry_file: Path,
) -> dict[str, object]:
    """Observe filenames only, without implying lock-holder or replay state."""
    result: dict[str, object] = {
        "schema_version": 1,
        "status": "unverified",
        "repository_count": None,
        "lease_file_count": None,
        "active_lease_count": None,
        "active_leases_verified": False,
        "warnings": [],
        "source": "local_files",
        "execution_authority": "not_asserted",
        "replay_authority": "not_asserted",
        "mutated": False,
    }
    try:
        ids = _repository_ids(_private_json(registry_file, REGISTRY_BYTES))
    except (OSError, ValueError, UnicodeError, TypeError):
        result["warnings"].append("registry_unavailable")
        return result
    result["repository_count"] = len(ids)
    try:
        count = _count_disk_lease_files(Path(state_root), no_projects=not ids)
    except (OSError, ValueError):
        result["warnings"].append("lease_inventory_unavailable")
        return result
    result["lease_file_count"] = count
    result["status"] = "observed"
    return result
