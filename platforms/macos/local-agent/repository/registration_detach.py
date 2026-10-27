"""Detach an already-disabled repository from only the local registry.

No checkout deletion, control-mailbox migration, credential revocation,
automatic git work or destructive cleanup is performed. The write is one
atomic registry replacement under the same offline exclusion as disable.
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
    _require_offline_installation, plan_repository_disable,
)
from local_agent.repository.transactional_add import _atomic_json, _registry_lock


def plan_repository_detach(
    repository_id: str, *, registry_path: Path | None = None,
    home: Path | None = None,
) -> dict[str, Any]:
    """Read-only registry-only detach proposal, requiring prior disable."""
    plan = plan_repository_disable(
        repository_id, registry_path=registry_path, home=home,
    )
    if not plan["already_disabled"]:
        raise ValueError("disable repository before planning detach")
    return {
        **plan,
        "operation": "detach_registration_only",
        "required_prior_state": "disabled",
        "preserved_resources": [
            "work_checkout", "control_mailbox", "checkpoint_storage",
            "canonical_binding_catalog",
        ],
        "recovery": "single_atomic_registry_write",
    }


def _require_settled_add_journal(
    path: Path, *, repository_id: str, repository: str,
    agent_binding: str | None, work: Path, control: Path, checkpoints: Path,
) -> None:
    """Never hide a partly provisioned repository behind a detach."""
    journal_dir = path.parent / "pending-repository-add"
    journal = journal_dir / f"{repository_id}.json"
    if journal_dir.is_symlink() or journal.is_symlink():
        raise RuntimeError("pending repository-add journal path is not trusted")
    if not journal.exists():
        return
    if not journal.is_file():
        raise RuntimeError("pending repository-add journal is not a regular file")
    payload = json.loads(journal.read_text(encoding="utf-8"))
    expected = {
        "repository_id": repository_id,
        "repository": repository,
        "agent_binding": agent_binding,
        "work": str(work),
        "control": str(control),
        "checkpoints": str(checkpoints),
    }
    if not isinstance(payload, dict) or any(
        payload.get(field) != value for field, value in expected.items()
    ):
        raise RuntimeError("pending repository-add journal identity mismatch")
    if payload.get("state") != "committed":
        raise RuntimeError("unreconciled repository-add journal; refuse detach")


def apply_repository_detach(
    repository_id: str, *, expected_sha256: str,
    registry_path: Path | None = None, home: Path | None = None,
    state_dir: Path | None = None,
) -> dict[str, Any]:
    """Atomically unregister an offline, disabled and quiescent project only."""
    if not isinstance(expected_sha256, str) or not _SHA256_RE.fullmatch(expected_sha256):
        raise ValueError("detach --apply requires the plan's exact SHA256")
    resolved_home = (home or Path.home()).expanduser().resolve(strict=False)
    target = registry_path or default_registry_path(resolved_home)
    installation_state = state_dir or default_registry_path(resolved_home).parent
    with _registry_lock(target), _require_offline_installation(installation_state):
        raw, snapshot = _registry_snapshot(target, resolved_home)
        if hashlib.sha256(raw).hexdigest() != expected_sha256:
            raise RuntimeError("registry changed since detach plan; replan")
        matched = [
            item for item in snapshot["repositories"]
            if item["repository_id"] == repository_id
        ]
        if len(matched) != 1:
            raise ValueError(f"repository id is not registered: {repository_id!r}")
        if matched[0]["enabled"]:
            raise ValueError("disable repository before detach")
        contexts = load_repository_registry(
            home=resolved_home, path=target, include_disabled=True, allow_empty=True,
        )
        selected = [item for item in contexts if item.repository_id == repository_id]
        if len(selected) != 1:
            raise ValueError("ambiguous registered repository")
        context = selected[0]
        with _hold_repository_leases(installation_state, selected):
            _require_settled_add_journal(
                target, repository_id=repository_id,
                repository=context.repository, agent_binding=context.agent_binding,
                work=context.work, control=context.control,
                checkpoints=context.checkpoints,
            )
            if target.read_bytes() != raw:
                raise RuntimeError("registry changed during detach transaction")
            payload = json.loads(raw)
            remaining = [
                item for item in payload["repositories"]
                if item["id"] != repository_id
            ]
            if len(remaining) != len(payload["repositories"]) - 1:
                raise ValueError("ambiguous registry detach identity")
            payload["repositories"] = remaining
            # Atomic+fsynced replacement is the only state mutation. If the
            # process crashes, status resolves either pre- or post-detach.
            _atomic_json(target, payload)
    return {
        "status": "detached", "repository_id": repository_id,
        "changes_applied": True, "registry_only": True,
        "workspaces_preserved": True, "binding_catalog_unchanged": True,
        "reload_required": True,
    }
