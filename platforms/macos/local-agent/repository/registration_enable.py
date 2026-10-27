"""Guarded offline re-enable of a previously registered optional project.

No checkout, branch, binding or historical task is created or replayed.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from local_agent.repository.context import (
    default_registry_path, load_repository_registry,
)
from local_agent.repository.registration_disable import (
    _SHA256_RE, _hold_repository_leases, _registry_snapshot,
    _require_offline_installation,
)
from local_agent.repository.transactional_add import _atomic_json, _registry_lock


def plan_repository_enable(
    repository_id: str, *, registry_path: Path | None = None,
    home: Path | None = None,
) -> dict[str, Any]:
    """Inspect existing registration; changes are always explicitly offline."""
    if not repository_id:
        raise ValueError("repository id is required")
    resolved = (home or Path.home()).expanduser().resolve(strict=False)
    target = registry_path or default_registry_path(resolved)
    raw, snapshot = _registry_snapshot(target, resolved)
    rows = [row for row in snapshot["repositories"]
            if row["repository_id"] == repository_id]
    if len(rows) != 1:
        raise ValueError(f"repository id is not registered: {repository_id!r}")
    return {
        "status": "planned_only",
        "repository_id": repository_id, "repository": rows[0]["repository"],
        "already_enabled": rows[0]["enabled"],
        "work_mode": rows[0]["work_mode"],
        "control_branch": rows[0]["control_branch"],
        "expected_sha256": hashlib.sha256(raw).hexdigest(),
        "required_quiescence": "exclusive_installation_daemon_lock_and_empty_durable_state",
        "active_claims": "not_checked", "active_leases": "not_checked",
        "changes_applied": False,
    }


def apply_repository_enable(
    repository_id: str, *, expected_sha256: str,
    registry_path: Path | None = None, home: Path | None = None,
    state_dir: Path | None = None,
) -> dict[str, Any]:
    """Atomically flip one flag only; never turn on a running worker."""
    if not isinstance(expected_sha256, str) or not _SHA256_RE.fullmatch(expected_sha256):
        raise ValueError("enable --apply requires the plan's exact SHA256")
    resolved = (home or Path.home()).expanduser().resolve(strict=False)
    target = registry_path or default_registry_path(resolved)
    state = state_dir or default_registry_path(resolved).parent
    with _registry_lock(target), _require_offline_installation(state):
        raw, snapshot = _registry_snapshot(target, resolved)
        if hashlib.sha256(raw).hexdigest() != expected_sha256:
            raise RuntimeError("registry changed since enable plan; replan")
        rows = [row for row in snapshot["repositories"]
                if row["repository_id"] == repository_id]
        if len(rows) != 1:
            raise ValueError(f"repository id is not registered: {repository_id!r}")
        contexts = load_repository_registry(
            home=resolved, path=target, include_disabled=True, allow_empty=True,
        )
        chosen = [context for context in contexts if context.repository_id == repository_id]
        if len(chosen) != 1:
            raise ValueError("ambiguous registry context")
        with _hold_repository_leases(state, chosen):
            if target.read_bytes() != raw:
                raise RuntimeError("registry changed during enable transaction")
            if rows[0]["enabled"]:
                return {
                    "status": "already_enabled", "repository_id": repository_id,
                    "changes_applied": False,
                }
            payload = json.loads(raw)
            selected = [row for row in payload["repositories"]
                        if row["id"] == repository_id]
            if len(selected) != 1:
                raise ValueError("ambiguous registry entry")
            selected[0]["enabled"] = True
            _atomic_json(target, payload)
        return {
            "status": "enabled", "repository_id": repository_id,
            "changes_applied": True, "workspaces_preserved": True,
            "reload_required": True,
        }
