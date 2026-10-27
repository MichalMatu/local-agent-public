"""Pure planning for user-selected project checkouts; never clone or edit.

This module is shared by future CLI/GUI onboarding. It deliberately
does not enroll a new remote task permission or mutate an existing Git tree.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from local_agent.repository.checkout_identity import validate_checkout_readonly
from local_agent.repository.binding import (
    DEFAULT_CATALOG_PATH, catalog_record_for_repository_if_present,
)
from local_agent.repository.context import (
    RepositoryContext, default_registry_path, load_repository_registry,
    validate_repository_set,
)


def _location(raw: str | Path | None, default: Path, *, cwd: Path) -> Path:
    if raw is None:
        return default.resolve(strict=False)
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = cwd / path
    return path.resolve(strict=False)


def _planned_checkout(path: Path, repository: RepositoryContext, label: str) -> str:
    if path.is_symlink():
        raise ValueError(f"{label} path must not be a symlink")
    if path.exists():
        if not path.is_dir():
            raise ValueError(f"{label} path exists but is not a directory")
        validate_checkout_readonly(path, repository, label)
        return "reuse_existing_checkout"
    return "clone_if_missing"


def plan_repository_add(
    repository: str,
    *,
    repository_id: str | None = None,
    work_dir: str | Path | None = None,
    control_dir: str | Path | None = None,
    checkpoints_dir: str | Path | None = None,
    registry_path: Path | None = None,
    catalog_path: Path | None = None,
    home: Path | None = None,
    cwd: Path | None = None,
) -> dict[str, Any]:
    """Dry-run optional project onboarding with custom/previously cloned code."""
    resolved_home = (home or Path.home()).expanduser().resolve(strict=False)
    current = (cwd or Path.cwd()).resolve(strict=False)
    raw_id = repository_id if repository_id is not None else repository.rsplit("/", 1)[-1].lower()
    root = resolved_home / "agent-workspace" / "repos" / raw_id
    candidate = {
        "id": raw_id,
        "repository": repository,
        "work_dir": str(_location(work_dir, root / "work", cwd=current)),
        "control_dir": str(_location(control_dir, root / "control", cwd=current)),
        "checkpoints_dir": str(_location(checkpoints_dir, root / "checkpoints", cwd=current)),
    }
    from local_agent.repository.context import repository_from_dict

    selected = repository_from_dict(candidate, home=resolved_home)
    target_registry = registry_path or default_registry_path(resolved_home)
    if target_registry.exists():
        existing = load_repository_registry(
            home=resolved_home, path=target_registry,
            include_disabled=True, allow_empty=True,
        )
    else:
        if registry_path is not None:
            raise ValueError("explicit registry file does not exist")
        existing = []
    validate_repository_set([*existing, selected], allow_empty=True)

    work_action = _planned_checkout(selected.work, selected, "work")
    control_action = _planned_checkout(selected.control, selected, "control")
    if selected.checkpoints.exists() and not selected.checkpoints.is_dir():
        raise ValueError("checkpoints destination is not a directory")
    catalog_file = catalog_path or DEFAULT_CATALOG_PATH
    binding = catalog_record_for_repository_if_present(
        selected.repository_id, selected.repository, path=catalog_file,
    )
    return {
        "status": "planned_only",
        "repository_id": selected.repository_id,
        "repository": selected.repository,
        "registry_path": str(target_registry),
        "work": {"path": str(selected.work), "action": work_action,
                 "planned_mode": "attached" if work_action == "reuse_existing_checkout"
                 else "managed"},
        "control": {"path": str(selected.control), "action": control_action},
        "checkpoints": {"path": str(selected.checkpoints), "action": "reuse_or_create_directory"},
        "catalog_binding": binding.agent_binding if binding is not None else None,
        "binding_action": "reuse_canonical_binding" if binding else "binding_onboarding_required",
        "existing_worktree_requires_safe_executor": work_action == "reuse_existing_checkout",
        "execution_ready": False,
        "already_registered": False,
        "changes_applied": False,
        "execution_authority": "not_asserted",
    }
