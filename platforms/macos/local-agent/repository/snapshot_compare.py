"""Bounded advisory comparison of two retained attached-task result trees.

This is NOT an owner-checkout diff, patch, conflict decision or promotion
authority. Inspect only private snapshots with no-follow directory FDs,
never invoke Git or execute a task-supplied hook/configuration.
"""
from __future__ import annotations

import hashlib
import itertools
import os
import stat
from pathlib import Path
from typing import Any

from local_agent.repository.snapshot_review import (
    _COMMIT,
    _FILE_FLAGS,
    _FINGERPRINT,
    _SNAPSHOT_ID,
    _checkpoint_directory,
    _directory,
    _manifest,
)

_MAX_ENTRIES = 128
_MAX_DEPTH = 12
_MAX_FILE_BYTES = 512 * 1024
_MAX_TOTAL_BYTES = 8 * 1024 * 1024
_MAX_PATH_BYTES = 512
_MAX_CHANGES = 128


def _read_file(parent: int, name: str, budget: dict[str, int]) -> tuple[int, str]:
    fd = os.open(name, _FILE_FLAGS, dir_fd=parent)
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or before.st_size > _MAX_FILE_BYTES:
            raise ValueError("snapshot contains an unsafe or oversized file")
        if budget["bytes"] + before.st_size > _MAX_TOTAL_BYTES:
            raise ValueError("snapshot comparison byte budget exhausted")
        digest = hashlib.sha256()
        size = 0
        while True:
            chunk = os.read(fd, min(65536, _MAX_FILE_BYTES + 1 - size))
            if not chunk:
                break
            size += len(chunk)
            if size > _MAX_FILE_BYTES or budget["bytes"] + size > _MAX_TOTAL_BYTES:
                raise ValueError("snapshot comparison byte budget exhausted")
            digest.update(chunk)
        after = os.fstat(fd)
        if (
            size != before.st_size
            or (before.st_ino, before.st_dev, before.st_size,
                before.st_mtime_ns, before.st_ctime_ns)
            != (after.st_ino, after.st_dev, after.st_size,
                after.st_mtime_ns, after.st_ctime_ns)
        ):
            raise ValueError("snapshot file changed while reading")
        budget["bytes"] += size
        return stat.S_IMODE(before.st_mode), digest.hexdigest()
    finally:
        os.close(fd)


def _collect(
    directory: int, prefix: str, depth: int,
    budget: dict[str, int], files: dict[str, tuple[int, str]],
) -> None:
    if depth > _MAX_DEPTH:
        raise ValueError("snapshot comparison depth budget exhausted")
    # A directory can be edited while scandir yields its entries. Its
    # own mtime/ctime detects additions, removals and renames during
    # traversal without ever following a path outside the pinned FD.
    directory_before = os.fstat(directory)
    with os.scandir(directory) as scan:
        # Never materialize an unbounded hostile directory.
        names = [entry.name for entry in itertools.islice(
            scan, _MAX_ENTRIES - budget["entries"] + 2,
        )]
    if len(names) > _MAX_ENTRIES - budget["entries"] + 1:
        raise ValueError("snapshot comparison entry budget exhausted")
    for name in sorted(names):
        if not prefix and name == ".git":
            # Neither parse nor execute copied repository metadata.
            continue
        if (
            name in ("", ".", "..", ".git")
            or not name.isprintable()
            or "/" in name
            or "\\" in name
        ):
            raise ValueError("snapshot contains an unsafe filename")
        relative = f"{prefix}/{name}" if prefix else name
        if len(os.fsencode(relative)) > _MAX_PATH_BYTES:
            raise ValueError("snapshot comparison path budget exhausted")
        budget["entries"] += 1
        if budget["entries"] > _MAX_ENTRIES:
            raise ValueError("snapshot comparison entry budget exhausted")
        info = os.stat(name, dir_fd=directory, follow_symlinks=False)
        if stat.S_ISDIR(info.st_mode):
            with _directory(name, parent=directory) as child:
                _collect(child, relative, depth + 1, budget, files)
        elif stat.S_ISREG(info.st_mode):
            files[relative] = _read_file(directory, name, budget)
        else:
            # Including symlinks, FIFOs and devices; no outward traversal.
            raise ValueError("snapshot contains an unsafe nonregular entry")
    directory_after = os.fstat(directory)
    if (
        directory_before.st_dev != directory_after.st_dev
        or directory_before.st_ino != directory_after.st_ino
        or directory_before.st_mtime_ns != directory_after.st_mtime_ns
        or directory_before.st_ctime_ns != directory_after.st_ctime_ns
    ):
        raise ValueError("snapshot directory changed during comparison")


def _snapshot(
    root: int, snapshot_id: str,
) -> tuple[str, str, dict[str, tuple[int, str]]]:
    with _directory(snapshot_id, parent=root) as task:
        source = _manifest(task, "snapshot.json")
        completion = _manifest(task, "completion.json")
        task_id = source.get("task_id")
        fingerprint = source.get("fingerprint")
        head = source.get("source_head")
        if (
            source.get("mode") != "attached_isolated"
            or not isinstance(task_id, str)
            or not 1 <= len(task_id) <= 128
            or not task_id.isprintable()
            or not isinstance(fingerprint, str)
            or not _FINGERPRINT.fullmatch(fingerprint)
            or not isinstance(head, str)
            or not _COMMIT.fullmatch(head)
            or completion.get("version") != 1
            or completion.get("state") != "completed_with_changes"
            or completion.get("task_id") != task_id
            or completion.get("source_fingerprint") != fingerprint
            or not isinstance(completion.get("result_fingerprint"), str)
            or not _FINGERPRINT.fullmatch(completion["result_fingerprint"])
            or completion.get("promotion") != "manual_only"
            or completion.get("owner_reconciliation") != "not_checked"
        ):
            raise ValueError("untrusted snapshot completion or source identity")
        files: dict[str, tuple[int, str]] = {}
        with _directory("work", parent=task) as work:
            _collect(work, "", 0, {"entries": 0, "bytes": 0}, files)
            # Cross-check the entire bounded private tree a second time.
            # An earlier file can change after its first read, without
            # changing the containing directory's mtime/ctime.
            rechecked: dict[str, tuple[int, str]] = {}
            _collect(work, "", 0, {"entries": 0, "bytes": 0}, rechecked)
            if files != rechecked:
                raise ValueError("snapshot changed between comparison passes")
        return head, fingerprint, files


def compare_attached_snapshots(
    checkpoints: Path, left_id: str, right_id: str,
) -> dict[str, Any]:
    """Show only changed relative paths between two *private* task results.

    Both completed task snapshots must declare the same starting fingerprint
    and source HEAD. No file bodies, digests or patch hunks are returned.
    """
    if (
        not isinstance(left_id, str)
        or not _SNAPSHOT_ID.fullmatch(left_id)
        or not isinstance(right_id, str)
        or not _SNAPSHOT_ID.fullmatch(right_id)
        or left_id == right_id
    ):
        raise ValueError("two distinct valid snapshot IDs are required")
    try:
        with _checkpoint_directory(checkpoints) as checkpoint:
            with _directory("attached-task-workspaces", parent=checkpoint) as root:
                left_head, left_start, left = _snapshot(root, left_id)
                right_head, right_start, right = _snapshot(root, right_id)
    except OSError as exc:
        raise ValueError("snapshot comparison root or entry is unsafe/unavailable") from exc
    if left_head != right_head or left_start != right_start:
        raise ValueError("snapshot comparisons require identical source fingerprints")
    changes = []
    for path in sorted(left.keys() | right.keys()):
        if path not in left:
            kind = "added"
        elif path not in right:
            kind = "removed"
        elif left[path] != right[path]:
            kind = "modified"
        else:
            continue
        changes.append({"path": path, "change": kind})
        if len(changes) > _MAX_CHANGES:
            raise ValueError("snapshot comparison output budget exhausted")
    return {
        "status": "snapshot_comparison_unverified",
        "left_snapshot_id": left_id,
        "right_snapshot_id": right_id,
        "changed_paths": changes,
        "changed_count": len(changes),
        "content_returned": False,
        "private_snapshot_contents_read": True,
        "owner_checkout_read": False,
        "owner_checkout_modified": False,
        "promotion_allowed": False,
        "owner_reconciliation": "not_checked",
        "identity_trust": "untrusted_local_metadata",
        "limits": {
            "entries_per_snapshot": _MAX_ENTRIES,
            "max_file_bytes": _MAX_FILE_BYTES,
            "max_total_bytes_per_snapshot": _MAX_TOTAL_BYTES,
            "max_depth": _MAX_DEPTH,
            "advisory_recheck_passes": 2,
        },
    }
