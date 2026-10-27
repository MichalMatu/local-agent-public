"""Read-only, optional B2-style project registry for independent Linux hosts.

This module has NO runtime dispatcher or Git side effects. In particular,
loading a registry NEVER enrolls repositories or enables project task intake.
Installed Debian / Termux command-only agents do not import this module.
"""
from __future__ import annotations

import json
import re
import secrets
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from linux_agent.repository.binding import canonical_agent_binding

_ID = re.compile(r"^[A-Za-z0-9._-]+$")
_REMOTE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_BRANCH = re.compile(r"^[A-Za-z0-9._/-]+$")
MAX_REGISTRY_BYTES = 256 * 1024
ENTRY_FIELDS = frozenset({
    "id", "repository", "agent_binding", "host_id",
    "host_mailbox_enabled", "host_execution_enabled",
    "host_identity_file", "control_dir", "work_dir",
    "checkpoints_dir", "work_mode", "control_branch",
    "default_branch", "legacy_workspace", "enabled",
})


@dataclass(frozen=True, slots=True)
class Project:
    """Source-only project identity; not permission to execute a task."""

    repository_id: str
    repository: str
    agent_binding: str | None
    control: Path
    work: Path
    checkpoints: Path
    work_mode: str
    control_branch: str
    default_branch: str
    host_id: str | None
    host_mailbox_enabled: bool
    host_execution_enabled: bool
    host_identity_file: Path | None


def _unique_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate registry key: {key}")
        result[key] = value
    return result


def _name(raw: object, field: str, pattern: re.Pattern[str]) -> str:
    if not isinstance(raw, str) or not raw or not pattern.fullmatch(raw):
        raise ValueError(f"invalid {field}")
    if raw in {".", ".."} or ".." in raw.split("/") or raw.startswith("/") or raw.endswith("/"):
        raise ValueError(f"invalid {field}")
    return raw


def _project_path(value: object, *, home: Path, default: Path, field: str) -> Path:
    if value is None:
        path = default
    elif isinstance(value, str):
        path = home / value[2:] if value.startswith("~/") else Path(value)
    else:
        raise ValueError(f"{field} must be a path string")
    if not path.is_absolute() or ".." in path.parts:
        raise ValueError(f"{field} must be an absolute canonical path")
    # Resolve source-only paths for exact overlap detection. The historical
    # B2 service may be mounted separately; nothing is created or altered.
    return path.resolve(strict=False)


def _entry(raw: object, *, home: Path, expected_host_id: str) -> Project:
    if not isinstance(raw, dict):
        raise ValueError("project registry entries must be objects")
    unsupported = set(raw) - ENTRY_FIELDS
    if unsupported:
        raise ValueError("unsupported project fields: " + ", ".join(sorted(unsupported)))
    rid = _name(raw.get("id"), "repository id", _ID)
    remote = _name(raw.get("repository"), "repository remote", _REMOTE)
    branch = _name(raw.get("control_branch", "agent-control"), "control branch", _BRANCH)
    default_branch = _name(raw.get("default_branch", "main"), "default branch", _BRANCH)
    work_mode = raw.get("work_mode", "managed")
    if type(work_mode) is not str or work_mode not in {"managed", "attached"}:
        raise ValueError("invalid project work mode")
    binding_raw = raw.get("agent_binding")
    binding = (canonical_agent_binding(binding_raw, field=f"{rid}.agent_binding")
               if binding_raw is not None else None)

    host_id = raw.get("host_id")
    if host_id is not None:
        host_id = _name(host_id, "project host id", _ID)
    mailbox = raw.get("host_mailbox_enabled", False)
    execution = raw.get("host_execution_enabled", False)
    if type(mailbox) is not bool or type(execution) is not bool:
        raise ValueError("project host mailbox/execution flags must be boolean")
    if mailbox and host_id != expected_host_id:
        raise ValueError("project host mailbox has wrong host identity")
    if mailbox and binding is None:
        raise ValueError("project mailbox requires existing project agent binding")
    if execution and not mailbox:
        raise ValueError("project host execution requires enabled project mailbox")

    identity_raw = raw.get("host_identity_file")
    identity = (_project_path(identity_raw, home=home, default=home,
                              field="host_identity_file")
                if identity_raw is not None else None)
    if execution and identity is None:
        raise ValueError("project host execution requires local identity file")
    legacy = raw.get("legacy_workspace", False)
    if type(legacy) is not bool:
        raise ValueError("legacy_workspace must be boolean")
    base = home / "agent-workspace" if legacy else home / "agent-workspace/repos" / rid
    paths = {label: _project_path(raw.get(key), home=home, default=base / label, field=key)
             for label, key in (("control", "control_dir"), ("work", "work_dir"),
                                ("checkpoints", "checkpoints_dir"))}
    return Project(rid, remote, binding, paths["control"], paths["work"],
                   paths["checkpoints"], work_mode, branch, default_branch,
                   host_id, mailbox, execution, identity)


def _overlap(a: Path, b: Path) -> bool:
    return a == b or a in b.parents or b in a.parents


def load_project_registry(
    path: Path, *, home: Path, expected_host_id: str,
    include_disabled: bool = False,
) -> tuple[Project, ...]:
    """Validate an *explicit* B2-compatible registry; [] is host-only.

    No default discovery, no implicit project, no filesystem/Git writes.
    Project execution continues to be rejected by the Linux host-only
    worker until a distinct durable project runner is implemented.
    """
    _name(expected_host_id, "host id", _ID)
    if type(include_disabled) is not bool:
        raise ValueError("include_disabled must be boolean")
    if not path.is_absolute() or path.is_symlink() or not path.is_file():
        raise ValueError("explicit regular project registry path required")
    if not home.is_absolute():
        raise ValueError("home must be absolute")
    size = path.stat().st_size
    if size > MAX_REGISTRY_BYTES:
        raise ValueError("project registry is oversized")
    try:
        with path.open("rb") as stream:
            blob = stream.read(MAX_REGISTRY_BYTES + 1)
        if len(blob) > MAX_REGISTRY_BYTES:
            raise ValueError("project registry is oversized")
        payload = json.loads(blob, object_pairs_hook=_unique_pairs)
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        raise ValueError("invalid project registry JSON") from exc
    if not isinstance(payload, dict) or type(payload.get("version")) is not int or payload["version"] != 1:
        raise ValueError("unsupported project registry version")
    raw_repositories = payload.get("repositories")
    if not isinstance(raw_repositories, list):
        raise ValueError("project registry requires repositories list")

    projects: list[Project] = []
    for raw in raw_repositories:
        if not isinstance(raw, dict):
            raise ValueError("project registry entries must be objects")
        enabled = raw.get("enabled", True)
        if type(enabled) is not bool:
            raise ValueError("project enabled must be boolean")
        if enabled or include_disabled:
            projects.append(_entry(raw, home=home, expected_host_id=expected_host_id))

    ids: set[str] = set()
    remotes: set[str] = set()
    bindings: set[str] = set()
    used_paths: list[Path] = []
    for project in projects:
        rid, remote = project.repository_id.casefold(), project.repository.casefold()
        if rid in ids or remote in remotes:
            raise ValueError("duplicate project repository id or remote")
        ids.add(rid)
        remotes.add(remote)
        if project.agent_binding is not None:
            if project.agent_binding in bindings:
                raise ValueError("duplicate project agent binding")
            bindings.add(project.agent_binding)
        for item in (project.control, project.work, project.checkpoints):
            if any(_overlap(item, earlier) for earlier in used_paths):
                raise ValueError("overlapping project paths")
            used_paths.append(item)
    return tuple(projects)


def resolve_project_identity(task: dict[str, Any], projects: tuple[Project, ...]) -> Project:
    """Identity check ONLY; this deliberately does NOT authorize execution.

    Never use this helper as a substitute for durable project claim,
    per-repository intake or a project-specific worker/STOP.
    """
    if not isinstance(task, dict):
        raise ValueError("project task must be an object")
    rid = _name(task.get("repository_id"), "task repository id", _ID)
    binding = canonical_agent_binding(task.get("agent_binding"))
    matches = [p for p in projects if p.repository_id == rid]
    if len(matches) != 1:
        raise ValueError("project repository not explicitly selected")
    project = matches[0]
    if project.agent_binding is None or not secrets.compare_digest(project.agent_binding, binding):
        raise ValueError("project agent binding mismatch")
    if not project.host_mailbox_enabled or not project.host_execution_enabled:
        raise ValueError("project host execution not enabled")
    return project
