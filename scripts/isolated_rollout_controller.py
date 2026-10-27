"""External, sandbox-only source-switch and rollback rehearsal.

No production control, daemon imports, service management or privilege path.
Source-only Phase B scaffolding; never an installation authorization.
"""
from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import tempfile
import time
from typing import Iterator

BASE_SHA = "cccc67fce71db02d0e878172b1e96ce858ee96ea"
CHANGED = frozenset({
    "local_agent/foundation/storage.py",
    "local_agent/repository/cleanup.py",
    "local_agent/repository/compaction.py",
    "local_agent/daemon/service.py",
})
ADDED = frozenset({
    "tests/test_control_rebase_guard.py",
    "tests/test_self_update_eligibility_logging.py",
})
MARKER = ".isolated-source-rollout-sandbox"
MARKER_VALUE = "local-agent-source-rollout-rehearsal-v1\n"
JOURNAL = "transaction.json"


class RolloutRejected(ValueError):
    """Fail-closed admission for a rehearsal transaction."""


def _sandbox(path: Path) -> Path:
    root = path.resolve(strict=True)
    temp_root = Path(tempfile.gettempdir()).resolve(strict=True)
    if root == temp_root or temp_root not in root.parents:
        raise RolloutRejected("rehearsal root must be beneath the OS temporary directory")
    if not root.name.startswith("la-rollout-"):
        raise RolloutRejected("rehearsal root must use a dedicated la-rollout- prefix")
    marker = root / MARKER
    if not marker.is_file() or marker.is_symlink() or marker.read_text() != MARKER_VALUE:
        raise RolloutRejected("missing exact isolated-rehearsal marker")
    return root


def _fsync_dir(directory: Path) -> None:
    fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _atomic_json(path: Path, data: dict) -> None:
    fd, name = tempfile.mkstemp(prefix=".transaction-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, sort_keys=True, indent=2)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(name, path)
        _fsync_dir(path.parent)
    finally:
        if os.path.exists(name):
            os.unlink(name)


@contextlib.contextmanager
def _locked(root: Path) -> Iterator[None]:
    root = _sandbox(root)
    with (root / ".transaction.lock").open("a+b") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def _tree_hashes(directory: Path) -> dict[str, str]:
    if not directory.is_dir() or directory.is_symlink():
        raise RolloutRejected("source tree must be a real directory")
    hashes = {}
    for path in sorted(directory.rglob("*")):
        if path.is_symlink():
            raise RolloutRejected(f"source symlink rejected: {path}")
        if path.is_dir():
            continue
        if not path.is_file() or not stat.S_ISREG(path.stat().st_mode):
            raise RolloutRejected(f"source special file rejected: {path}")
        rel = path.relative_to(directory).as_posix()
        if rel.startswith(".git/") or rel == ".git":
            raise RolloutRejected("Git metadata must not enter immutable source snapshot")
        hashes[rel] = hashlib.sha256(path.read_bytes()).hexdigest()
    if not hashes:
        raise RolloutRejected("empty source snapshot")
    return hashes


def _tree_modes(directory: Path) -> dict[str, int]:
    modes = {".": stat.S_IMODE(directory.stat().st_mode)}
    for item in directory.rglob("*"):
        modes[item.relative_to(directory).as_posix()] = stat.S_IMODE(item.stat().st_mode)
    return modes


def _write_protected_tree(path: Path) -> None:
    for item in sorted(path.rglob("*"), key=lambda p: len(p.parts), reverse=True):
        mode = item.stat().st_mode
        item.chmod(0o555 if item.is_dir() or (mode & 0o111) else 0o444)
    path.chmod(0o555)


def _pointer(root: Path) -> str:
    pointer = root / "current"
    if not pointer.is_symlink():
        raise RolloutRejected("source pointer is not a symlink")
    value = os.readlink(pointer)
    if value not in {"snapshots/baseline", "snapshots/candidate"}:
        raise RolloutRejected("source pointer is outside transaction snapshots")
    return value


def _set_pointer(root: Path, name: str) -> None:
    if name not in {"baseline", "candidate"}:
        raise RolloutRejected("invalid snapshot target")
    temporary = root / ".current.new"
    if temporary.exists() or temporary.is_symlink():
        raise RolloutRejected("unfinished pointer switch")
    temporary.symlink_to("snapshots/" + name)
    os.replace(temporary, root / "current")
    _fsync_dir(root)


def _journal(root: Path) -> dict:
    return json.loads((root / JOURNAL).read_text(encoding="utf-8"))


def prepare(root: Path, baseline: Path, candidate: Path) -> dict:
    """Rehearse exact 4-replaced+2-added overlay; never install either tree."""
    with _locked(root):
        root = _sandbox(root)
        if (root / JOURNAL).exists() or (root / "snapshots").exists():
            raise RolloutRejected("transaction is already initialized")
        original = _tree_hashes(baseline)
        newer = _tree_hashes(candidate)
        replaced = {p for p in original.keys() & newer.keys() if original[p] != newer[p]}
        added = newer.keys() - original.keys()
        removed = original.keys() - newer.keys()
        if replaced != CHANGED or added != ADDED or removed:
            raise RolloutRejected(f"overlay mismatch: changed={sorted(replaced)} added={sorted(added)} removed={sorted(removed)}")
        if root in baseline.resolve().parents or root in candidate.resolve().parents:
            raise RolloutRejected("input snapshots must be outside the transaction root")
        snapshots = root / "snapshots"
        snapshots.mkdir()
        modes = {}
        try:
            for name, source, expected in (("baseline", baseline, original), ("candidate", candidate, newer)):
                dest = snapshots / name
                shutil.copytree(source, dest, symlinks=False)
                if _tree_hashes(dest) != expected:
                    raise RolloutRejected("copied immutable snapshot digest mismatch")
                _write_protected_tree(dest)
                modes[name] = _tree_modes(dest)
            _set_pointer(root, "baseline")
            state = {"version": 1, "state": "prepared", "base_sha": BASE_SHA,
                     "changed": sorted(CHANGED), "added": sorted(ADDED),
                     "baseline_hashes": original, "candidate_hashes": newer,
                     "baseline_modes": modes["baseline"], "candidate_modes": modes["candidate"]}
            _atomic_json(root / JOURNAL, state)
            return state
        except BaseException:
            if not (root / JOURNAL).exists():
                (root / "current").unlink(missing_ok=True)
                shutil.rmtree(snapshots)
            raise


def _verify_snapshots(root: Path, journal: dict) -> None:
    for name in ("baseline", "candidate"):
        directory = root / "snapshots" / name
        if (_tree_hashes(directory) != journal[name + "_hashes"]
                or _tree_modes(directory) != journal[name + "_modes"]):
            raise RolloutRejected("snapshot content or permissions changed after preparation")


def _safe_queue(before: dict) -> None:
    if set(before) != {"daemon_state", "active_tasks", "pending_tasks", "unreconciled_unknown", "source_sha"}:
        raise RolloutRejected("incomplete before-state/queue snapshot")
    if before["daemon_state"] != "idle" or before["source_sha"] != BASE_SHA:
        raise RolloutRejected("service not quiescent or source SHA mismatch")
    for field in ("active_tasks", "pending_tasks", "unreconciled_unknown"):
        if type(before[field]) is not int or before[field] != 0:
            raise RolloutRejected("unsettled queue/claim prevents a source switch")


def arm(root: Path, before: dict, *, watchdog_seconds: int = 30) -> dict:
    """Atomic rehearsal switch; journal exists before pointer movement."""
    with _locked(root):
        root = _sandbox(root)
        state = _journal(root)
        if state["state"] != "prepared" or _pointer(root) != "snapshots/baseline":
            raise RolloutRejected("source switch is not in prepared state")
        if type(watchdog_seconds) is not int or not 1 <= watchdog_seconds <= 300:
            raise RolloutRejected("invalid bounded watchdog timeout")
        _safe_queue(before)
        _verify_snapshots(root, state)
        state.update(state="armed", before=before.copy(), deadline_epoch=time.time() + watchdog_seconds)
        _atomic_json(root / JOURNAL, state)
        _set_pointer(root, "candidate")
        state["state"] = "switched_unverified"
        _atomic_json(root / JOURNAL, state)
        return state


def _rollback_locked(root: Path, journal: dict, *, reason: str) -> dict:
    if journal["state"] == "accepted":
        raise RolloutRejected("accepted transaction cannot be implicitly rolled back")
    _pointer(root)
    baseline = root / "snapshots" / "baseline"
    if (_tree_hashes(baseline) != journal["baseline_hashes"]
            or _tree_modes(baseline) != journal["baseline_modes"]):
        raise RolloutRejected("trusted rollback baseline has changed")
    _set_pointer(root, "baseline")
    journal.update(state="rolled_back", rollback_reason=reason)
    _atomic_json(root / JOURNAL, journal)
    return journal


def watchdog(root: Path, health: dict | None = None, *, now_epoch: float | None = None) -> dict:
    """External one-shot watchdog decision; absent evidence rolls back fail-closed."""
    with _locked(root):
        root = _sandbox(root)
        state = _journal(root)
        if state["state"] not in {"armed", "switched_unverified"}:
            raise RolloutRejected("no unaccepted transaction to inspect")
        if _pointer(root) not in {"snapshots/baseline", "snapshots/candidate"}:
            raise RolloutRejected("pointer tampered")
        try:
            _verify_snapshots(root, state)
        except RolloutRejected:
            return _rollback_locked(root, state, reason="candidate_snapshot_integrity_failed")
        if (now_epoch if now_epoch is not None else time.time()) > state["deadline_epoch"]:
            return _rollback_locked(root, state, reason="watchdog_deadline_expired")
        if health != {"service_active": True, "heartbeat_fresh": True,
                      "source_digest_ok": True, "queues_safe": True}:
            return _rollback_locked(root, state, reason="independent_health_not_accepted")
        if _pointer(root) != "snapshots/candidate":
            return _rollback_locked(root, state, reason="candidate_pointer_missing")
        state["state"] = "accepted"
        _atomic_json(root / JOURNAL, state)
        return state


def recover_unverified(root: Path) -> dict:
    """Crash recovery never resumes an unaccepted switch or task execution."""
    with _locked(root):
        root = _sandbox(root)
        state = _journal(root)
        if state["state"] in {"armed", "switched_unverified"}:
            return _rollback_locked(root, state, reason="unverified_interrupted_transaction")
        return state
