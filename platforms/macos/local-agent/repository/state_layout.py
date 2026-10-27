"""Prepare private repository state at registration or admitted recovery.

Read-only observations never repair missing state and continue to report
incomplete records as UNKNOWN.
"""
from __future__ import annotations

import os
import re
import secrets
import stat
from collections.abc import Callable
from pathlib import Path

from local_agent.repository.snapshot_review import _checkpoint_directory

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


def prepare_repository_state(state_root: Path, repository_id: str, *, create: bool) -> None:
    """Publish a complete empty ledger before a new registration becomes visible.

    The caller holds the registry lock. Existing state is only validated: a
    missing child may represent lost history and must never be recreated.
    Interrupted preparation leaves an unpublished private staging directory;
    explicit retry can create another one without touching claims or results.
    """
    if (not state_root.is_absolute() or repository_id in (".", "..")
            or not _SAFE_PART.fullmatch(repository_id)):
        raise ValueError("invalid repository state identity")
    if create:
        state_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    with _checkpoint_directory(state_root) as root_fd:
        _private_dir(root_fd)
        if create:
            parent_fd = _enter_directory(root_fd, "repositories")
            os.fsync(root_fd)
        else:
            parent_fd = os.open("repositories", _FLAGS, dir_fd=root_fd)
        try:
            _private_dir(parent_fd)
            try:
                project_fd = os.open(repository_id, _FLAGS, dir_fd=parent_fd)
            except FileNotFoundError:
                if not create:
                    raise RuntimeError(f"project ledger missing: {repository_id}") from None
                staged = ".new-ledger-" + secrets.token_hex(16)
                os.mkdir(staged, mode=0o700, dir_fd=parent_fd)
                project_fd = os.open(staged, _FLAGS, dir_fd=parent_fd)
                try:
                    for leaf in ("claims", "result-spool", "corrupt-claims"):
                        child_fd = _enter_directory(project_fd, leaf)
                        try:
                            os.fsync(child_fd)
                        finally:
                            os.close(child_fd)
                    os.fsync(project_fd)
                    os.rename(staged, repository_id, src_dir_fd=parent_fd,
                              dst_dir_fd=parent_fd)
                    os.fsync(parent_fd)
                except BaseException:
                    os.close(project_fd)
                    raise
            try:
                _private_dir(project_fd)
                for leaf in ("claims", "result-spool", "corrupt-claims"):
                    child_fd = os.open(leaf, _FLAGS, dir_fd=project_fd)
                    try:
                        _private_dir(child_fd)
                    finally:
                        os.close(child_fd)
            finally:
                os.close(project_fd)
        finally:
            os.close(parent_fd)


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
