"""Disposable sandbox admission-fence model; NOT a production scheduler lock.

Only test writers in this sandbox cooperate with this lock. No running
Local Agent process uses it, so it does not authorize real service updates.
"""
from __future__ import annotations

import contextlib
import fcntl
import json
import os
from pathlib import Path
import re
from typing import Iterator

from scripts import isolated_rollout_controller as rollout

_ID = re.compile(r"^[A-Za-z0-9._-]{1,80}$")
_FENCE = "admission-frozen.json"


@contextlib.contextmanager
def _gate(root: Path) -> Iterator[Path]:
    root = rollout._sandbox(root)
    path = root / ".admission.lock"
    if path.is_symlink():
        raise rollout.RolloutRejected("sandbox admission lock is a symlink")
    with path.open("a+b") as fd:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise rollout.RolloutRejected("simulated admission gate is busy") from exc
        try:
            yield root
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)


def _pending(root: Path) -> int:
    inbox = root / "simulated-inbox"
    if inbox.is_symlink() or not inbox.is_dir():
        raise rollout.RolloutRejected("sandbox simulated inbox missing")
    count = 0
    for item in inbox.iterdir():
        if item.is_symlink() or not item.is_file() or item.suffix != ".json":
            raise rollout.RolloutRejected("unsafe simulated inbox entry")
        count += 1
    return count


def admit_simulated_task(root: Path, name: str) -> None:
    """Simulated task intake, never real Git/host task delivery."""
    if not isinstance(name, str) or _ID.fullmatch(name) is None:
        raise rollout.RolloutRejected("invalid simulated task id")
    with _gate(root) as sandbox:
        marker = sandbox / _FENCE
        if marker.exists() or marker.is_symlink():
            raise rollout.RolloutRejected("admission frozen; task not claimed")
        target = sandbox / "simulated-inbox" / (name + ".json")
        if _pending(sandbox) > 1000:
            raise rollout.RolloutRejected("sandbox inbox exceeds bound")
        descriptor = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(descriptor, "w") as stream:
            stream.write(json.dumps({"id": name}) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        rollout._fsync_dir(target.parent)


def freeze_and_switch_simulation(root: Path, before: dict, *,
                                 watchdog_seconds: int = 30) -> dict:
    """Persist a no-admission fence BEFORE the sandbox pointer changes.

    A crash leaves a durable freeze, requiring separate reconciliation.
    The supplied before-state remains synthetic; production effects forbidden.
    """
    with _gate(root) as sandbox:
        if _pending(sandbox):
            raise rollout.RolloutRejected("simulated scheduler has pending tasks")
        if (sandbox / _FENCE).exists() or (sandbox / _FENCE).is_symlink():
            raise rollout.RolloutRejected("simulated admission already frozen")
        rollout._safe_queue(before)
        fd = os.open(sandbox / _FENCE, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as handle:
            handle.write('{"state":"frozen","sandbox_only":true}\n')
            handle.flush()
            os.fsync(handle.fileno())
        rollout._fsync_dir(sandbox)
        # If arm fails or the process crashes, do not silently remove the
        # fence or try to replay a task. Explicit reconciliation is required.
        return rollout.arm(sandbox, before, watchdog_seconds=watchdog_seconds)
