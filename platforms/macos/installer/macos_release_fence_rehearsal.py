"""EXPLICITLY SANDBOX-ONLY producer for the Mac admission-fence protocol.

There is intentionally no production state-path CLI and NO unfreeze command.
The installed daemon is not enrolled; this helper cannot make it quiescent.
"""
from __future__ import annotations

import fcntl
import json
import os
from pathlib import Path
import stat
import sys

from local_agent.supervisor import macos_release_admission as admission
from scripts import isolated_rollout_controller as rollout

STATE_SUBDIR = "macos-rehearsal-state"
_FROZEN_DATA = b'{"version":1,"state":"frozen","sandbox_only":true}\n'


def _private_state(root: Path) -> Path:
    state = root / STATE_SUBDIR
    if state.is_symlink() or not state.is_dir():
        raise rollout.RolloutRejected("isolated Mac admission state directory missing")
    info = state.lstat()
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
            or info.st_mode & 0o077 or info.st_nlink < 2):
        raise rollout.RolloutRejected("isolated Mac state directory not private")
    if state.resolve(strict=True).parent != root:
        raise rollout.RolloutRejected("isolated Mac state escapes sandbox root")
    return state


def freeze_disposable_mac_admission(root: Path) -> dict:
    """Persist freeze after exclusive lease proves no cooperating task remains.

    Idempotent automatic thaw/replay is deliberately unavailable. The calling
    operator must separately reconcile task results and obtain live approval.
    """
    if sys.platform != "darwin":
        raise rollout.RolloutRejected("macOS-only admission freeze rehearsal")
    root = rollout._sandbox(root)
    state = _private_state(root)
    # Require separately staged, private opt-in with a strict schema.
    if not admission._active_config(state):
        raise rollout.RolloutRejected("isolated Mac admission was not explicitly enabled")
    lock = state / admission.LOCK
    if lock.is_symlink():
        raise rollout.RolloutRejected("isolated Mac admission lock symlink rejected")
    try:
        fd = os.open(lock, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
    except OSError as exc:
        raise rollout.RolloutRejected("isolated Mac lock cannot be opened") from exc
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_mode & 0o077 or info.st_nlink != 1):
            raise rollout.RolloutRejected("isolated Mac admission lock is unsafe")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (BlockingIOError, OSError) as exc:
            raise rollout.RolloutRejected("active Mac test task lease prevents freeze") from exc
        try:
            # Recheck under exclusive lock. This closes the controller's
            # admission race against every *cooperating* task worker.
            if not admission._active_config(state):
                raise rollout.RolloutRejected("Mac opt-in changed before exclusive freeze")
            marker = state / admission.FROZEN
            if marker.exists() or marker.is_symlink():
                raise rollout.RolloutRejected("Mac admission already frozen")
            handle = os.open(
                marker, os.O_WRONLY | os.O_CREAT | os.O_EXCL
                | getattr(os, "O_NOFOLLOW", 0), 0o600,
            )
            with os.fdopen(handle, "wb") as stream:
                stream.write(_FROZEN_DATA)
                stream.flush()
                os.fsync(stream.fileno())
            rollout._fsync_dir(state)
            # No release of this durable barrier on return. Do not claim
            # anything about workers that have not opted into admission.
            return {
                "version": 1,
                "sandbox_only": True,
                "exclusive_lease_acquired": True,
                "admission_frozen": True,
                "intake_reopened": False,
                "tasks_replayed": False,
                "live_mac_admission_authorized": False,
            }
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def stage_disposable_opt_in(root: Path) -> Path:
    """Set up a private test-only opt-in in a marked disposable sandbox."""
    if sys.platform != "darwin":
        raise rollout.RolloutRejected("macOS-only admission setup rehearsal")
    root = rollout._sandbox(root)
    target = root / STATE_SUBDIR
    if target.is_symlink():
        raise rollout.RolloutRejected("isolated Mac admission state symlink rejected")
    target.mkdir(mode=0o700, exist_ok=False)
    config = target / admission.CONFIG
    descriptor = os.open(
        config, os.O_CREAT | os.O_EXCL | os.O_WRONLY
        | getattr(os, "O_NOFOLLOW", 0), 0o600,
    )
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        json.dump({"version": 1, "enabled": True}, stream, separators=(",", ":"))
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    rollout._fsync_dir(target)
    rollout._fsync_dir(root)
    return target
