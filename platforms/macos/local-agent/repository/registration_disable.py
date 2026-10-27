"""Fail-closed offline repository disable; never erase owner or transport files.

A running supervisor holds the installation's agentd.lock. Registry disable
must acquire that same exclusive lock and prove durable admission state is
empty. This is an *offline* operation, not hot reload or runtime cancellation.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import resource
from contextlib import contextmanager
from collections.abc import Iterable
from pathlib import Path
from typing import Any, Iterator

from local_agent.foundation.process import execution_lease_path
from local_agent.repository.context import (
    RepositoryContext, default_registry_path, load_repository_registry,
    repository_lease_keys,
)
from local_agent.repository.registration_status import repository_registration_status
from local_agent.repository.transactional_add import _atomic_json, _registry_lock

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_QUIESCENT_DIRS = ("claims", "corrupt-claims", "result-spool")


def _registry_snapshot(path: Path, home: Path) -> tuple[bytes, dict[str, Any]]:
    if path.is_symlink() or not path.is_file():
        raise ValueError("disable requires an existing regular registry")
    raw = path.read_bytes()
    snapshot = repository_registration_status(registry_path=path, home=home)
    # Registry must not change between validation and digest capture.
    if raw != path.read_bytes():
        raise RuntimeError("registry changed during disable preflight")
    return raw, snapshot


def plan_repository_disable(
    repository_id: str, *, registry_path: Path | None = None,
    home: Path | None = None,
) -> dict[str, Any]:
    """Compute a write-once expected digest; do not assert daemon quiescence."""
    if not repository_id:
        raise ValueError("repository id is required")
    resolved_home = (home or Path.home()).expanduser().resolve(strict=False)
    target = registry_path or default_registry_path(resolved_home)
    raw, snapshot = _registry_snapshot(target, resolved_home)
    selected = [
        row for row in snapshot["repositories"]
        if row["repository_id"] == repository_id
    ]
    if len(selected) != 1:
        raise ValueError(f"repository id is not registered: {repository_id!r}")
    return {
        "status": "planned_only",
        "repository_id": repository_id,
        "repository": selected[0]["repository"],
        "already_disabled": not selected[0]["enabled"],
        "registry_path": str(target),
        "expected_sha256": hashlib.sha256(raw).hexdigest(),
        "required_quiescence": "exclusive_installation_daemon_lock_and_empty_durable_state",
        "active_claims": "not_checked",
        "active_leases": "not_checked",
        "changes_applied": False,
        "execution_authority": "not_asserted",
    }



def _host_claims_have_terminal_results(state_dir: Path) -> None:
    """Host claims are durable *history*, not the active-claim queue.

    A host claim is retained after publication for at-most-once execution.
    Only an incomplete, untrusted or mismatched result blocks an offline
    registry change. No record is removed or rewritten.
    """
    claims = state_dir / "host-tasks" / "claims"
    if claims.is_symlink():
        raise RuntimeError("unresolved durable host claims; unsafe symlink")
    if not claims.exists():
        return
    results = state_dir / "host-tasks" / "results"
    if not claims.is_dir() or results.is_symlink() or not results.is_dir():
        raise RuntimeError("unresolved durable host claims; results unavailable")
    for claim in claims.iterdir():
        if claim.name == ".gitkeep":
            continue
        result = results / claim.name
        if (claim.is_symlink() or claim.suffix != ".json"
                or not claim.is_file() or result.is_symlink()
                or not result.is_file()):
            raise RuntimeError("unresolved durable host claims; missing terminal result")
        try:
            if claim.stat().st_size > 16384 or result.stat().st_size > 2_000_000:
                raise ValueError("oversized host receipt")
            entry = json.loads(claim.read_text(encoding="utf-8"))
            terminal = json.loads(result.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, ValueError) as exc:
            raise RuntimeError("unresolved durable host claims; corrupt receipt") from exc
        if (not isinstance(entry, dict) or not isinstance(terminal, dict)
                or entry.get("id") != claim.stem
                or entry.get("status") != "claimed"
                or terminal.get("id") != entry["id"]
                or terminal.get("task_digest") != entry.get("task_digest")
                or terminal.get("target_host_id") != entry.get("target_host_id")
                or terminal.get("target_installation_id") != entry.get("target_installation_id")
                or terminal.get("status") not in ("done", "failed")):
            raise RuntimeError("unresolved durable host claims; result mismatch")




@contextmanager
def _require_offline_installation(state_dir: Path) -> Iterator[None]:
    """Hold the same singleton lock as the supervisor, not a status timestamp."""
    if state_dir.is_symlink() or not state_dir.is_dir():
        raise RuntimeError("installation state directory is not trusted")
    lock = state_dir / "agentd.lock"
    if lock.is_symlink() or not lock.is_file():
        raise RuntimeError("installation daemon lock is unavailable; cannot prove offline")
    flags = os.O_RDWR
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(lock, flags)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("installation daemon is active; stop and drain it first") from exc
        for name in _QUIESCENT_DIRS:
            directory = state_dir / name
            if directory.is_symlink() or not directory.is_dir():
                raise RuntimeError(f"{name} state cannot be verified")
            with os.scandir(directory) as entries:
                if next(entries, None) is not None:
                    raise RuntimeError(f"unresolved durable {name} state; refuse disable")
        _host_claims_have_terminal_results(state_dir)
        yield
    finally:
        os.close(fd)


@contextmanager
def _hold_repository_leases(
    state_dir: Path, repositories: Iterable[RepositoryContext],
) -> Iterator[None]:
    """Hold every repository identity lease across the registry commit."""
    lock_dir = state_dir / "locks" / "repositories"
    if lock_dir.is_symlink():
        raise RuntimeError("repository lease directory must not be a symlink")
    lock_dir.mkdir(parents=True, exist_ok=True)
    if not lock_dir.is_dir():
        raise RuntimeError("repository lease directory cannot be verified")
    keys = {
        key for repository in repositories for key in repository_lease_keys(repository)
    }
    # The Mac's default soft RLIMIT_NOFILE can be just 256. Each
    # registered project owns several independent identities. Raise only
    # this maintenance process's temporary soft limit when the hard
    # limit permits; never silently skip a lease to bypass the cap.
    soft_limit, hard_limit = resource.getrlimit(resource.RLIMIT_NOFILE)
    requested = max(soft_limit, len(keys) + 256)
    effective_hard = requested if hard_limit == resource.RLIM_INFINITY else hard_limit
    if requested > effective_hard:
        requested = effective_hard
    increased = requested > soft_limit
    if increased:
        resource.setrlimit(resource.RLIMIT_NOFILE, (requested, hard_limit))
    handles: list[int] = []
    try:
        for key in sorted(keys):
            path = execution_lease_path(lock_dir, key)
            if path.is_symlink():
                raise RuntimeError("repository execution lease path must not be a symlink")
            flags = os.O_CREAT | os.O_RDWR
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            fd = os.open(path, flags, 0o600)
            handles.append(fd)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise RuntimeError("repository execution lease is busy") from exc
        yield
    finally:
        for fd in reversed(handles):
            os.close(fd)
        if increased:
            resource.setrlimit(resource.RLIMIT_NOFILE, (soft_limit, hard_limit))


def apply_repository_disable(
    repository_id: str, *, expected_sha256: str,
    registry_path: Path | None = None, home: Path | None = None,
    state_dir: Path | None = None,
) -> dict[str, Any]:
    """Atomically disable only after an offline exclusion and digest precondition."""
    if not isinstance(expected_sha256, str) or not _SHA256_RE.fullmatch(expected_sha256):
        raise ValueError("disable --apply requires the plan's exact SHA256")
    resolved_home = (home or Path.home()).expanduser().resolve(strict=False)
    target = registry_path or default_registry_path(resolved_home)
    installation_state = state_dir or default_registry_path(resolved_home).parent
    with _registry_lock(target), _require_offline_installation(installation_state):
        raw, snapshot = _registry_snapshot(target, resolved_home)
        if hashlib.sha256(raw).hexdigest() != expected_sha256:
            raise RuntimeError("registry changed since disable plan; replan")
        matching = [
            row for row in snapshot["repositories"]
            if row["repository_id"] == repository_id
        ]
        if len(matching) != 1:
            raise ValueError(f"repository id is not registered: {repository_id!r}")
        contexts = load_repository_registry(
            home=resolved_home, path=target,
            include_disabled=True, allow_empty=True,
        )
        selected_contexts = [
            context for context in contexts if context.repository_id == repository_id
        ]
        if len(selected_contexts) != 1:
            raise ValueError("ambiguous registry context")
        with _hold_repository_leases(installation_state, selected_contexts):
            if target.read_bytes() != raw:
                raise RuntimeError("registry changed during disable transaction")
            if not matching[0]["enabled"]:
                return {
                    "status": "already_disabled", "repository_id": repository_id,
                    "changes_applied": False,
                }
            payload = json.loads(raw)
            entries = payload["repositories"]
            selected = [item for item in entries if item["id"] == repository_id]
            if len(selected) != 1:
                raise ValueError("ambiguous registry entry")
            selected[0]["enabled"] = False
            _atomic_json(target, payload)
        return {
            "status": "disabled", "repository_id": repository_id,
            "changes_applied": True, "workspaces_preserved": True,
            "reload_required": True,
        }
