"""Fail-closed host-targeted intake at the existing repository worker boundary.

This is an explicit, opt-in admission boundary, not a new scheduler or claim path.
Legacy queues can never be a host-mailbox fallback. Only a repository with
one trusted operator-assigned host may execute addressed work; there is no
cross-host execution lease, shared writable repo or identity-only grant.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from local_agent.repository.context import RepositoryContext
from local_agent.repository.host_routing import (
    HostEnrollment,
    HostIdentity,
    require_host_task_target,
)
from local_agent.runtime.task_contract import require_task_agent_binding, task_digest

HOST_TARGET_FIELDS = frozenset({
    "host_mailbox_version", "target_host_id", "target_installation_id",
    "target_repository_id",
})
HOST_MAILBOX_VERSION = 1
# No environment variable, task field or operator status may turn this on.
HOST_TARGETED_INTAKE_ENABLED = False


class HostAdmissionBlocked(ValueError):
    """Named terminal pre-claim refusal, without changing the task payload."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


def has_host_target(task: Mapping[str, Any]) -> bool:
    """Even one routing field makes a legacy queue item host-targeted."""
    return any(field in task for field in HOST_TARGET_FIELDS)


def preflight_host_targeted_task(
    task: Mapping[str, Any],
    repository: RepositoryContext,
    *,
    source: str = "legacy_queue",
    enabled: bool = HOST_TARGETED_INTAKE_ENABLED,
    operator_disabled: bool = False,
    local_identity: HostIdentity | None = None,
    enrollment: HostEnrollment | None = None,
    expected_task_id: str | None = None,
    expected_task_digest: str | None = None,
    lifecycle_state: str = "queued",
    single_owner_verified: bool = False,
) -> None:
    """Validate immutable intent for one verified owner; deny legacy fallback.

    A trusted caller must obtain single_owner_verified from the current operator
    Git ref; self-reported host metadata or task fields cannot confer it.
    """
    if operator_disabled:
        raise HostAdmissionBlocked("operator_emergency_stop")
    if not has_host_target(task):
        return  # The original queue and its scheduling ownership are unchanged.
    if source != "host_mailbox":
        raise HostAdmissionBlocked("host_target_in_legacy_queue")
    if not enabled:
        raise HostAdmissionBlocked("host_targeted_intake_disabled")
    if lifecycle_state != "queued":
        raise HostAdmissionBlocked("host_task_not_replayable")
    if set(HOST_TARGET_FIELDS) - set(task):
        raise HostAdmissionBlocked("host_target_incomplete")
    if type(task.get("host_mailbox_version")) is not int or task["host_mailbox_version"] != HOST_MAILBOX_VERSION:
        raise HostAdmissionBlocked("host_mailbox_version_invalid")
    if not isinstance(expected_task_id, str) or task.get("id") != expected_task_id:
        raise HostAdmissionBlocked("host_task_identity_changed")
    if not isinstance(expected_task_digest, str) or task_digest(dict(task)) != expected_task_digest:
        raise HostAdmissionBlocked("host_task_digest_changed")
    try:
        if repository.agent_binding is None:
            raise ValueError("repository is unbound")
        require_task_agent_binding(dict(task), repository.agent_binding)
        if not isinstance(local_identity, HostIdentity) or not isinstance(enrollment, HostEnrollment):
            raise ValueError("trusted local identity and operator enrollment are unavailable")
        require_host_task_target(
            task, local_identity=local_identity, enrollment=enrollment,
            repository=repository,
        )
    except ValueError as exc:
        raise HostAdmissionBlocked("host_binding_or_enrollment_invalid") from exc
    # No two enabled hosts may own the same writable binding. This is NOT a
    # remotely coordinated lease; multi-owner execution remains prohibited.
    if not single_owner_verified:
        raise HostAdmissionBlocked("distributed_fence_unavailable")
