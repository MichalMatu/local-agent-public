"""Bounded, descriptor-anchored review of untrusted attached-task snapshots.

The inventory is never a verified diff, execution authorization or permission
for promotion. No Git commands, task replay or owner-checkout writes occur.
"""
from __future__ import annotations

import itertools
import json
import os
import re
import stat
import sys
from contextlib import ExitStack, contextmanager
from pathlib import Path
from typing import Any, Iterator

_FINGERPRINT = re.compile(r"^[0-9a-f]{64}$")
_COMMIT = re.compile(r"^[0-9a-f]{40}$")
_MAX_MANIFEST_BYTES = 16384
_MAX_ENTRIES = 100
_SNAPSHOT_ID = re.compile(r"^task-[A-Za-z0-9._-]{1,128}$")
_DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
_FILE_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK


@contextmanager
def _directory(name: str | os.PathLike[str], *, parent: int | None = None) -> Iterator[int]:
    """Pin a directory inode; refuse a symlink even if its name is swapped."""
    fd = os.open(name, _DIRECTORY_FLAGS, dir_fd=parent)
    try:
        yield fd
    finally:
        os.close(fd)



@contextmanager
def _checkpoint_directory(path: Path) -> Iterator[int]:
    """Walk *every* path component without following parent symlinks.

    O_NOFOLLOW on the final checkpoints leaf alone does not protect an
    attacker-controlled ancestor. Pin each parent inode before opening its
    child. macOS /var, /tmp and /etc are OS-managed /private aliases;
    traverse their literal /private target without following the alias.
    """
    parts = path.parts
    if ".." in parts:
        raise ValueError("checkpoint path contains unsafe parent traversal")
    if sys.platform == "darwin" and path.is_absolute() and len(parts) > 1:
        alias = parts[1]
        if alias in ("var", "tmp", "etc"):
            try:
                target = os.readlink("/" + alias)
            except OSError:
                pass
            else:
                if target in ("private/" + alias, "/private/" + alias):
                    parts = ("/", "private", alias, *parts[2:])
    with ExitStack() as opened:
        parent = opened.enter_context(_directory("/" if path.is_absolute() else "."))
        for component in (parts[1:] if path.is_absolute() else parts):
            if component == ".":
                continue
            parent = opened.enter_context(_directory(component, parent=parent))
        yield parent


def _manifest(parent: int, name: str) -> dict[str, Any]:
    """Read at most one small regular file without following a link or FIFO."""
    fd = os.open(name, _FILE_FLAGS, dir_fd=parent)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > _MAX_MANIFEST_BYTES:
            raise ValueError("snapshot manifest is not a bounded regular file")
        with os.fdopen(fd, "rb", closefd=False) as handle:
            raw = handle.read(_MAX_MANIFEST_BYTES + 1)
        if len(raw) > _MAX_MANIFEST_BYTES:
            raise ValueError("snapshot manifest exceeds size limit")
        payload = json.loads(raw.decode("utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("snapshot manifest must be an object")
        return payload
    finally:
        os.close(fd)


def _record(root: int, name: str, root_path: Path) -> dict[str, Any]:
    record: dict[str, Any] = {
        "snapshot_id": name,
        "local_path": str(root_path / name),
        "review_state": "unverified",
        "promotion_allowed": False,
    }
    if not name.startswith("task-"):
        record["reason"] = "unexpected_snapshot_entry"
        return record
    try:
        with _directory(name, parent=root) as task:
            try:
                with _directory("work", parent=task):
                    pass  # Never inspect or execute worktree contents.
            except OSError:
                record["reason"] = "isolated_worktree_missing_or_unsafe"
                return record
            try:
                manifest = _manifest(task, "snapshot.json")
                fingerprint = manifest.get("fingerprint")
                task_id = manifest.get("task_id")
                source_head = manifest.get("source_head")
                if (
                    manifest.get("mode") != "attached_isolated"
                    or not isinstance(fingerprint, str)
                    or not _FINGERPRINT.fullmatch(fingerprint)
                    or not isinstance(task_id, str)
                    or not 1 <= len(task_id) <= 128
                    or not task_id.isprintable()
                    or not isinstance(source_head, str)
                    or not _COMMIT.fullmatch(source_head)
                ):
                    raise ValueError("invalid source snapshot identity")
                record["task_id"] = task_id
                record["source_head"] = source_head
                try:
                    # lstat-like check: a dangling symlink is NOT unfinished.
                    os.stat("completion.json", dir_fd=task, follow_symlinks=False)
                except FileNotFoundError:
                    record["review_state"] = "interrupted_or_unfinished"
                else:
                    outcome = _manifest(task, "completion.json")
                    if (
                        outcome.get("version") != 1
                        or outcome.get("state") != "completed_with_changes"
                        or outcome.get("task_id") != task_id
                        or outcome.get("source_fingerprint") != fingerprint
                        or not isinstance(outcome.get("result_fingerprint"), str)
                        or not _FINGERPRINT.fullmatch(outcome["result_fingerprint"])
                        or outcome.get("promotion") != "manual_only"
                        or outcome.get("owner_reconciliation") != "not_checked"
                    ):
                        raise ValueError("invalid completion identity")
                    record["review_state"] = "completed_with_changes"
            except (OSError, ValueError, UnicodeError, json.JSONDecodeError):
                record["reason"] = "untrusted_or_invalid_manifest"
    except OSError:
        record["reason"] = "unexpected_snapshot_entry"
    return record


def inspect_attached_snapshots(
    checkpoints: Path, *, limit: int = 25,
    snapshot_id: str | None = None,
) -> dict[str, Any]:
    """Inventory retained private results, never an approval for write-back."""
    if type(limit) is not int or not 1 <= limit <= _MAX_ENTRIES:
        raise ValueError("snapshot review limit must be between 1 and 100")
    if snapshot_id is not None and (
        not isinstance(snapshot_id, str)
        or not _SNAPSHOT_ID.fullmatch(snapshot_id)
    ):
        raise ValueError("invalid attached snapshot id")
    try:
        with _checkpoint_directory(checkpoints) as checkpoint_fd:
            try:
                with _directory("attached-task-workspaces", parent=checkpoint_fd) as root:
                    root_path = checkpoints / "attached-task-workspaces"
                    if snapshot_id is not None:
                        # Direct inode-anchored lookup; a requested snapshot
                        # remains reachable even beyond the bounded list.
                        try:
                            os.stat(snapshot_id, dir_fd=root, follow_symlinks=False)
                        except FileNotFoundError:
                            records = []
                        else:
                            records = [_record(root, snapshot_id, root_path)]
                        truncated = False
                    else:
                        with os.scandir(root) as scan:
                            candidates = list(itertools.islice(scan, limit + 1))
                        truncated = len(candidates) > limit
                        records = [
                            _record(root, entry.name, root_path)
                            for entry in sorted(candidates[:limit], key=lambda item: item.name)
                        ]
            except FileNotFoundError:
                return _response(checkpoints, [], truncated=False)
    except FileNotFoundError:
        return _response(checkpoints, [], truncated=False)
    except OSError as exc:
        raise ValueError("checkpoint root is unavailable or unsafe") from exc
    return _response(checkpoints, records, truncated=truncated)


def _response(
    checkpoints: Path, records: list[dict[str, Any]], *, truncated: bool,
) -> dict[str, Any]:
    return {
        "status": "snapshot_inventory_only",
        "checkpoints_dir": str(checkpoints),
        "snapshots": records,
        "returned": len(records),
        "truncated": truncated,
        "worktree_contents_read": False,
        "owner_checkout_modified": False,
        "promotion_allowed": False,
        "manifest_trust": "untrusted_local_metadata",
    }
