"""Pure read-only inspection of the optional local repository registry.

This is a configuration snapshot, NOT a daemon/lease/claim/authorization probe.
Disabled entries are checked for identity collisions just like enabled entries.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from local_agent.repository.context import (
    REGISTRY_VERSION, default_registry_path, repository_from_dict,
    validate_repository_set,
)


def repository_registration_status(
    *, registry_path: Path | None = None, repository_id: str | None = None,
    home: Path | None = None,
) -> dict[str, Any]:
    """Report every registration without Git, writes, or daemon communication."""
    resolved_home = (home or Path.home()).expanduser().resolve(strict=False)
    target = registry_path or default_registry_path(resolved_home)
    if target.is_symlink():
        raise ValueError("repository registry must not be a symlink")
    if not target.exists():
        if registry_path is not None:
            raise ValueError(f"explicit repository registry does not exist: {target}")
        records: list[dict[str, Any]] = []
        configured = False
    else:
        if not target.is_file():
            raise ValueError("repository registry must be a regular file")
        payload = json.loads(target.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("repository registry root must be an object")
        if type(payload.get("version")) is not int or payload["version"] != REGISTRY_VERSION:
            raise ValueError("unsupported repository registry version")
        items = payload.get("repositories")
        if not isinstance(items, list):
            raise ValueError("repository registry repositories must be a list")
        records = []
        contexts = []
        for item in items:
            if not isinstance(item, dict):
                raise ValueError("repository registry entries must be objects")
            enabled = item.get("enabled", True)
            if type(enabled) is not bool:
                raise ValueError("repository enabled must be a boolean")
            context = repository_from_dict(item, home=resolved_home)
            contexts.append(context)
            records.append({
                "repository_id": context.repository_id,
                "repository": context.repository,
                "enabled": enabled,
                "work_mode": context.work_mode,
                "agent_binding": context.agent_binding,
                "control_branch": context.control_branch,
                "default_branch": context.default_branch,
                "work_dir": str(context.work),
                "control_dir": str(context.control),
                "checkpoints_dir": str(context.checkpoints),
                "checkpoints_declared_dir": str(item.get("checkpoints_dir")
                                                if item.get("checkpoints_dir") is not None
                                                else context.checkpoints),
            })
        # Reject ambiguity even if the conflicting registration is disabled.
        validate_repository_set(contexts, allow_empty=True)
        configured = True

    if repository_id is not None:
        records = [record for record in records if record["repository_id"] == repository_id]
        if not records:
            raise ValueError(f"repository id is not registered: {repository_id!r}")

    enabled_count = sum(record["enabled"] for record in records)
    return {
        "status": "configured" if configured else "unconfigured",
        "registry_path": str(target),
        "repositories": records,
        "counts": {
            "total": len(records),
            "enabled": enabled_count,
            "disabled": len(records) - enabled_count,
        },
        "observation_scope": "registry_only",
        "active_claims": "not_checked",
        "active_leases": "not_checked",
        "execution_authority": "not_asserted",
        "changes_applied": False,
    }
