"""Opt-in macOS release admission fence shared by task execution entrypoints.

This is only an admission primitive, NOT an update/install authority. The
installed baseline has no opt-in and does not honor this fence. A release
controller must be separately authorized and must verify every live intake
route, the installed source, and service liveness before any source switch.
"""
from __future__ import annotations

import contextlib
import fcntl
import json
import os
from pathlib import Path
import stat
import sys
from typing import Iterator

CONFIG = "macos-release-admission.json"
LOCK = "macos-release-admission.lock"
FROZEN = "macos-release-admission-frozen.json"
_CONFIG_LIMIT = 1024
REQUIRED_ENV = "LOCAL_AGENT_MACOS_ADMISSION_REQUIRED"


def _require_enrollment() -> bool:
    """Opt-in startup policy inherited by every Mac task subprocess.

    Only the exact value 1 enables the requirement. Invalid values must
    never silently weaken admission on a source selected for rollout.
    The separately protected service launch policy must pin this variable;
    an old installed daemon does not implement it.
    """
    raw = os.environ.get(REQUIRED_ENV)
    if raw not in (None, "0", "1"):
        raise MacReleaseAdmissionBlocked("invalid Mac admission requirement")
    return raw == "1"


class MacReleaseAdmissionBlocked(RuntimeError):
    """A private release fence prevents task effects; never consume the task."""


def _private_regular(path: Path, *, byte_limit: int) -> bytes:
    if path.is_symlink():
        raise MacReleaseAdmissionBlocked("unsafe macOS release admission symlink")
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except OSError as exc:
        raise MacReleaseAdmissionBlocked("macOS release admission record unavailable") from exc
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= byte_limit
                or info.st_uid != os.getuid() or info.st_mode & 0o077):
            raise MacReleaseAdmissionBlocked("macOS release admission record is not private")
        with os.fdopen(fd, "rb", closefd=False) as handle:
            data = handle.read(byte_limit + 1)
    finally:
        os.close(fd)
    if len(data) > byte_limit:
        raise MacReleaseAdmissionBlocked("macOS release admission record is oversized")
    return data


def _unique_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise MacReleaseAdmissionBlocked("duplicate macOS release admission field")
        result[key] = value
    return result


def _active_config(state_dir: Path) -> bool:
    if sys.platform != "darwin":
        return False
    if not state_dir.is_absolute() or state_dir.is_symlink():
        raise MacReleaseAdmissionBlocked("unsafe macOS state root")
    required = _require_enrollment()
    config = state_dir / CONFIG
    marker = state_dir / FROZEN
    # A leftover freeze marker is an admission barrier even when an operator
    # accidentally removes or disables its opt-in config.
    if not config.exists() and not config.is_symlink():
        if marker.exists() or marker.is_symlink():
            raise MacReleaseAdmissionBlocked("orphaned macOS release freeze marker")
        if required:
            raise MacReleaseAdmissionBlocked("required Mac release admission is not enrolled")
        return False
    try:
        payload = json.loads(
            _private_regular(config, byte_limit=_CONFIG_LIMIT).decode("utf-8"),
            object_pairs_hook=_unique_pairs,
        )
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise MacReleaseAdmissionBlocked("invalid macOS release admission config") from exc
    if (not isinstance(payload, dict) or set(payload) != {"version", "enabled"}
            or type(payload["version"]) is not int or payload["version"] != 1
            or type(payload["enabled"]) is not bool):
        raise MacReleaseAdmissionBlocked("invalid macOS release admission schema")
    if not payload["enabled"]:
        if marker.exists() or marker.is_symlink():
            raise MacReleaseAdmissionBlocked("macOS release freeze cannot be disabled")
        if required:
            raise MacReleaseAdmissionBlocked("required Mac release admission is disabled")
        return False
    return True


@contextlib.contextmanager
def macos_task_admission(state_dir: Path) -> Iterator[None]:
    """Hold a shared OS lock throughout one task turn and reject a freeze.

    An external authorized controller closes new admission with an owned,
    durable marker, lets admitted turns finish, then holds the exclusive
    lock through the switch. A process killed while holding this shared lease cannot
    leave a reusable task claim; existing UNKNOWN/no-replay rules apply.
    This module deliberately provides no freeze/unfreeze/install command.
    """
    state_dir = Path(state_dir)
    if not _active_config(state_dir):
        yield
        return
    lock_path = state_dir / LOCK
    if lock_path.is_symlink():
        raise MacReleaseAdmissionBlocked("macOS release lock symlink rejected")
    try:
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
    except OSError as exc:
        raise MacReleaseAdmissionBlocked("macOS release lock unavailable") from exc
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_mode & 0o077):
            raise MacReleaseAdmissionBlocked("macOS release lock ownership or mode invalid")
        try:
            fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except (BlockingIOError, OSError) as exc:
            raise MacReleaseAdmissionBlocked("macOS release exclusive fence is active") from exc
        marker = state_dir / FROZEN
        if marker.exists() or marker.is_symlink():
            # Verify that the marker is private; never treat a corrupt one as
            # an authorization to clear the freeze or execute tasks.
            _private_regular(marker, byte_limit=_CONFIG_LIMIT)
            raise MacReleaseAdmissionBlocked("macOS release admission is frozen")
        # A drain marker blocks later turns. This admitted turn can finish;
        # the controller must acquire the exclusive lease before switching.
        yield
    finally:
        os.close(fd)
