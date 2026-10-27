"""Offline preparation of host-addressed task bundles using existing task transport.

This module does not enroll an installation, publish to GitHub, claim a task,
relax admission policy or create an alternative task scheduler.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Sequence

from local_agent.repository.binding import AgentBindingRecord, resolve_execution_target
from local_agent.repository.path_safety import has_untrusted_symlink
from local_agent.repository.host_routing import (
    HostEnrollment,
    canonical_host_id,
    host_task_path,
    require_host_task_target,
)
from local_agent.runtime.task_contract import validate_task
from local_agent.runtime.task_preparation import prepare_task
from local_agent.runtime.task_transport import write_task_bundle

HOST_TASK_ENVELOPE_VERSION = 1
_TARGET_FIELDS = frozenset({
    'host_mailbox_version', 'target_host_id', 'target_installation_id',
    'target_repository_id',
})


def prepare_host_mailbox_task(
    draft: dict[str, Any],
    *,
    host_id: str,
    repository: str,
    enrollments: Sequence[HostEnrollment],
    profile: str = 'repository',
    catalog_path: Path | None = None,
) -> tuple[dict[str, Any], HostEnrollment, AgentBindingRecord]:
    """Bind a draft to one explicitly enrolled host and one canonical repository.

    The caller must obtain enrollments from an authenticated, pinned operator
    snapshot. No self-published host record is ever an enrollment source.
    """
    canonical_host_id(host_id)
    if not isinstance(draft, dict):
        raise ValueError('task draft must be an object')
    if _TARGET_FIELDS.intersection(draft):
        raise ValueError('task draft must not supply host mailbox routing fields')
    target = resolve_execution_target(repository, path=catalog_path)
    if len(enrollments) > 32 or not all(isinstance(item, HostEnrollment) for item in enrollments):
        raise ValueError('operator enrollments must be a validated bounded snapshot')
    matches = [item for item in enrollments if item.identity.host_id == host_id]
    if len(matches) != 1 or not matches[0].enabled:
        raise ValueError('target host is not enrolled and enabled')
    enrollment = matches[0]
    task = prepare_task(draft, repository=target.repository_id, profile=profile, catalog_path=catalog_path)
    task.update({
        'host_mailbox_version': HOST_TASK_ENVELOPE_VERSION,
        'target_host_id': enrollment.identity.host_id,
        'target_installation_id': enrollment.identity.installation_id,
        'target_repository_id': target.repository_id,
    })
    require_host_task_target(task, local_identity=enrollment.identity,
                             enrollment=enrollment, repository=target)
    validate_task(task, require_agent_binding=True)
    return task, enrollment, target


def write_host_mailbox_bundle(
    publication_root: Path,
    task: dict[str, Any],
    *,
    enrollment: HostEnrollment,
    target: AgentBindingRecord,
    protected_paths: Sequence[Path] = (),
) -> Path:
    """Stage one task under .agent/hosts in a safe, never-live publication root.

    The Git caller must commit/publish task payloads and manifest together and
    the future consumer must recheck enrollment, policy, and all bindings.
    """
    if not isinstance(task, dict) or type(task.get('host_mailbox_version')) is not int or task['host_mailbox_version'] != HOST_TASK_ENVELOPE_VERSION:
        raise ValueError('unsupported host mailbox task envelope')
    require_host_task_target(task, local_identity=enrollment.identity,
                             enrollment=enrollment, repository=target)
    validate_task(task, require_agent_binding=True)
    root = Path(publication_root).expanduser()
    if not root.is_absolute():
        raise ValueError('publication root must be absolute')
    if has_untrusted_symlink(root):
        raise ValueError('publication root must not contain symlinks')
    absolute = root.resolve()
    if absolute == Path(absolute.anchor):
        raise ValueError('publication root must be an isolated staging directory')
    if any((parent / '.git').exists() for parent in (root, *root.parents)):
        raise ValueError('host mailbox staging cannot be inside a Git working tree')
    for protected in protected_paths:
        protected_resolved = Path(protected).resolve()
        if absolute == protected_resolved or absolute in protected_resolved.parents or protected_resolved in absolute.parents:
            raise ValueError('host mailbox staging cannot overlap an active agent workspace')
    tasks_relative = host_task_path(enrollment.identity.host_id, task['id']).parent
    current = root
    for component in tasks_relative.parts:
        current = current / component
        if current.is_symlink():
            raise ValueError('host mailbox staging path contains a symlink')
    return write_task_bundle(root / tasks_relative, task)
