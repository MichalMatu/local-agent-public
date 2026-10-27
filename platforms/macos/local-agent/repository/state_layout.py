"""Initialize a repository quarantine inside the designated private state root.

Only admitted workers call this code; read-only observations never repair
missing state and continue to report incomplete records as UNKNOWN.
"""
from __future__ import annotations

import os
import re
import stat
from collections.abc import Callable
from pathlib import Path

_SAFE_PART = re.compile(r"^[A-Za-z0-9._-]+$")
_FLAGS = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)


def _private_dir(fd: int) -> None:
    info = os.fstat(fd)
    if (not stat.S_ISDIR(info.st_mode)
            or info.st_uid != os.getuid() or info.st_mode & 0o022):
        raise ValueError("unsafe repository state directory")


def _enter_directory(parent_fd: int, name: str) -> int:
    """Create/open one component without following any intermediate symlink."""
    try:
        os.mkdir(name, mode=0o700, dir_fd=parent_fd)
    except FileExistsError:
        pass
    child_fd = os.open(name, _FLAGS, dir_fd=parent_fd)
    try:
        _private_dir(child_fd)
    except BaseException:
        os.close(child_fd)
        raise
    return child_fd


def recover_claims_with_quarantine_layout(
    recover: Callable[[], None], corrupt_claims_dir: Path, state_root: Path,
) -> None:
    """Verify every child of the configured root before creating quarantine.

    Existing claims, quarantines, and spools are never deleted or repaired.
    The recovery callback is invoked only after an untrusted path is rejected.
    """
    root = Path(state_root)
    target = Path(corrupt_claims_dir)
    if not root.is_absolute() or not target.is_absolute():
        raise ValueError("repository state paths must be absolute")
    try:
        parts = target.relative_to(root).parts
    except ValueError as exc:
        raise ValueError("quarantine path escapes configured state root") from exc
    if (len(parts) not in (3, 5)
            or parts[0] != "repositories" or parts[-1] != "corrupt-claims"
            or (len(parts) == 5 and parts[2] != "hosts")
            or any(part in (".", "..") or not _SAFE_PART.fullmatch(part)
                   for part in parts)):
        raise ValueError("invalid quarantine directory identity")

    # The configured root is trusted input; never follow a link at its leaf.
    # Children, including repositories/<id>/hosts/<host>, are fd-anchored.
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    current_fd = os.open(root, _FLAGS)
    try:
        _private_dir(current_fd)
        for part in parts:
            next_fd = _enter_directory(current_fd, part)
            os.close(current_fd)
            current_fd = next_fd
    finally:
        os.close(current_fd)
    recover()
