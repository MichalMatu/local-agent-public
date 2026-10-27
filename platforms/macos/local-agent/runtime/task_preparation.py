"""Compile planner drafts into the existing immutable execution contract."""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

from local_agent.repository.binding import resolve_execution_target
from local_agent.runtime.task_contract import (
    require_task_agent_binding,
    task_resources_for,
    validate_task,
)

EXECUTION_PROFILES = ("repository", "hardware", "host-maintenance")


def prepare_task(
    draft: dict[str, Any],
    *,
    repository: str,
    profile: str = "repository",
    catalog_path: Path | None = None,
) -> dict[str, Any]:
    """Fill target identity and resource defaults without changing explicit inputs."""
    if not isinstance(draft, dict):
        raise ValueError("task draft must be an object")
    if profile not in EXECUTION_PROFILES:
        raise ValueError(f"unknown execution profile: {profile!r}")
    target = resolve_execution_target(repository, path=catalog_path)
    task = copy.deepcopy(draft)
    if "agent_binding" in task:
        require_task_agent_binding(task, target.agent_binding)
    task["agent_binding"] = target.agent_binding
    if "resources" in task:
        task_resources_for(task)
    if profile == "host-maintenance":
        if target.repository_id != "local-agent":
            raise ValueError("host-maintenance requires the local-agent target")
        # Host scope does not imply whole-machine exclusivity. Callers must request
        # "machine" explicitly for true whole-host operations, or name the concrete
        # external resources they need. Software-only maintenance stays concurrent.
        task.setdefault("resources", [])
    elif profile == "hardware":
        if not task.get("resources") or "machine" in task["resources"]:
            raise ValueError("hardware requires explicit named resources")
    else:
        task.setdefault("resources", [])
    validate_task(task, require_agent_binding=True)
    return task
