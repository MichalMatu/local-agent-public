"""One guarded offline repository-pool transaction; no project deletion.

A separate macOS controller owns service stop/start and independent
recovery. This Core module only changes enabled flags with an exact
backup and never stops a daemon itself.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import secrets
import stat
from typing import Any, Iterable

from local_agent.repository.context import default_registry_path, load_repository_registry
from local_agent.repository.registration_disable import (
    _SHA256_RE, _hold_repository_leases, _registry_snapshot,
    _require_offline_installation,
)
from local_agent.repository.transactional_add import _registry_lock


def _identities(ids: Iterable[str]) -> frozenset[str]:
    if isinstance(ids, str):
        raise ValueError("enabled repositories must be a collection, not a string")
    chosen = tuple(ids)
    if any(not isinstance(item, str) or not item for item in chosen):
        raise ValueError("invalid enabled repository identity")
    if len({item.casefold() for item in chosen}) != len(chosen):
        raise ValueError("duplicate enabled repository identity")
    return frozenset(chosen)


def plan_repository_selection(
    enabled_ids: Iterable[str], *, registry_path: Path | None = None,
    home: Path | None = None,
) -> dict[str, Any]:
    """Pure read-only plan. Zero and arbitrary-size project sets are valid."""
    resolved = (home or Path.home()).expanduser().resolve(strict=False)
    target = registry_path or default_registry_path(resolved)
    raw, status = _registry_snapshot(target, resolved)
    selected = _identities(enabled_ids)
    known = {row["repository_id"] for row in status["repositories"]}
    if not selected <= known:
        raise ValueError(f"unknown repository identities: {sorted(selected - known)}")
    before = {row["repository_id"] for row in status["repositories"] if row["enabled"]}
    return {
        "status": "planned_only", "enabled": sorted(selected),
        "previous_enabled": sorted(before),
        "to_enable": sorted(selected - before),
        "to_disable": sorted(before - selected),
        "total": len(known), "expected_sha256": hashlib.sha256(raw).hexdigest(),
        "changes_applied": False, "requires_offline": True,
    }


def _check_project_ledgers(state: Path, ids: Iterable[str]) -> None:
    """Every registration, including disabled, must have no durable work."""
    parent = state / "repositories"
    identities = tuple(ids)
    if parent.is_symlink() or (parent.exists() and not parent.is_dir()):
        raise RuntimeError("project ledgers cannot be verified")
    if not identities:
        # Host-only installations need no per-project ledger tree.
        return
    if not parent.is_dir():
        raise RuntimeError("project ledgers cannot be verified")
    for ident in identities:
        root = parent / ident
        if root.is_symlink() or not root.is_dir():
            raise RuntimeError(f"project ledger missing: {ident}")
        for leaf in ("claims", "result-spool", "corrupt-claims"):
            path = root / leaf
            if path.is_symlink():
                raise RuntimeError(f"unsafe project ledger: {ident}/{leaf}")
            if leaf == "corrupt-claims" and not path.exists():
                continue
            if not path.is_dir() or any(p.name != ".gitkeep" for p in path.iterdir()):
                raise RuntimeError(f"unresolved project ledger: {ident}/{leaf}")


def _trusted_backup(path: Path, *, expected_sha256: str) -> bytes:
    if path.is_symlink() or not path.is_file():
        raise RuntimeError("independent registry backup missing or linked")
    metadata = path.stat()
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > 4_000_000:
        raise RuntimeError("independent registry backup untrusted")
    data = path.read_bytes()
    if hashlib.sha256(data).hexdigest() != expected_sha256:
        raise RuntimeError("independent registry backup does not match original")
    return data


def _backup_once(path: Path, data: bytes) -> None:
    parent = path.parent
    if parent.is_symlink() or not parent.is_dir():
        raise RuntimeError("independent backup directory unavailable")
    metadata = parent.stat()
    if metadata.st_uid != os.getuid() or metadata.st_mode & 0o077:
        raise RuntimeError("independent backup directory must be private")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags, 0o600)
    try:
        with os.fdopen(fd, "wb", closefd=False) as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
    finally:
        os.close(fd)
    directory_fd = os.open(parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def _replace_raw(path: Path, contents: bytes) -> None:
    if path.is_symlink() or not path.is_file():
        raise RuntimeError("registry changed into an untrusted filesystem object")
    parent = path.parent
    temporary = parent / (".registry-recovery-" + secrets.token_hex(16))
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(temporary, flags, 0o600)
    try:
        with os.fdopen(fd, "wb", closefd=False) as out:
            out.write(contents)
            out.flush()
            os.fsync(out.fileno())
        os.replace(temporary, path)
        parent_fd = os.open(parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(parent_fd)
        finally:
            os.close(parent_fd)
    finally:
        os.close(fd)
        temporary.unlink(missing_ok=True)


def apply_repository_selection(
    enabled_ids: Iterable[str], *, expected_sha256: str,
    backup_path: Path, registry_path: Path | None = None,
    home: Path | None = None, state_dir: Path | None = None,
) -> dict[str, Any]:
    """CAS update after daemon exclusion, full durable drain, and backup."""
    if not isinstance(expected_sha256, str) or not _SHA256_RE.fullmatch(expected_sha256):
        raise ValueError("selection requires the plan's exact SHA256")
    selected = _identities(enabled_ids)
    resolved = (home or Path.home()).expanduser().resolve(strict=False)
    target = registry_path or default_registry_path(resolved)
    state = state_dir or default_registry_path(resolved).parent
    with _registry_lock(target), _require_offline_installation(state):
        raw, snapshot = _registry_snapshot(target, resolved)
        if hashlib.sha256(raw).hexdigest() != expected_sha256:
            raise RuntimeError("registry changed since selection plan")
        known = {row["repository_id"] for row in snapshot["repositories"]}
        if not selected <= known:
            raise ValueError("unknown repository selected")
        contexts = load_repository_registry(home=resolved, path=target,
                                             include_disabled=True, allow_empty=True)
        with _hold_repository_leases(state, contexts):
            _check_project_ledgers(state, known)
            if target.read_bytes() != raw:
                raise RuntimeError("registry changed during selection")
            if backup_path.exists() or backup_path.is_symlink():
                _trusted_backup(backup_path, expected_sha256=expected_sha256)
            else:
                _backup_once(backup_path, raw)
            payload = json.loads(raw)
            for row in payload["repositories"]:
                row["enabled"] = row["id"] in selected
            updated = (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode()
            _replace_raw(target, updated)
            return {
                "status": "applied", "enabled": sorted(selected),
                "previous_sha256": expected_sha256,
                "applied_sha256": hashlib.sha256(updated).hexdigest(),
                "backup_path": str(backup_path), "workspaces_preserved": True,
            }


def restore_repository_selection(
    *, expected_applied_sha256: str, original_sha256: str,
    backup_path: Path, registry_path: Path | None = None,
    home: Path | None = None, state_dir: Path | None = None,
) -> dict[str, Any]:
    """Byte-exact offline restore; never undo an unrelated registry change."""
    if any(not isinstance(value, str) or not _SHA256_RE.fullmatch(value)
           for value in (expected_applied_sha256, original_sha256)):
        raise ValueError("restore requires both exact registry hashes")
    resolved = (home or Path.home()).expanduser().resolve(strict=False)
    target = registry_path or default_registry_path(resolved)
    state = state_dir or default_registry_path(resolved).parent
    with _registry_lock(target), _require_offline_installation(state):
        raw, _snapshot = _registry_snapshot(target, resolved)
        digest = hashlib.sha256(raw).hexdigest()
        if digest == original_sha256:
            return {"status": "already_restored", "changes_applied": False}
        if digest != expected_applied_sha256:
            raise RuntimeError("registry has unrelated changes; refuse restore")
        contexts = load_repository_registry(home=resolved, path=target,
                                             include_disabled=True, allow_empty=True)
        with _hold_repository_leases(state, contexts):
            original = _trusted_backup(backup_path, expected_sha256=original_sha256)
            _check_project_ledgers(state, (c.repository_id for c in contexts))
            if target.read_bytes() != raw:
                raise RuntimeError("registry changed during restore")
            _replace_raw(target, original)
        return {"status": "restored", "changes_applied": True,
                "original_sha256": original_sha256}
