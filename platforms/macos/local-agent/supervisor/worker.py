#!/usr/bin/env python3
from __future__ import annotations

import contextlib
import json
from collections.abc import Iterator
from pathlib import Path

import local_agent.foundation.core as core
import local_agent.operator.local as agent_operator
import local_agent.daemon.service as agentd
import local_agent.repository.worker as serial_worker
from local_agent.repository.binding import validate_repository_control_binding
from local_agent.repository.host_admission import HostAdmissionBlocked
from local_agent.repository.host_execution import (
    HostAdmissionUnavailable, check_host_task_admission,
)
from local_agent.foundation.process import ExecutionLeaseBusy
from local_agent.repository.context import RepositoryContext
from local_agent.runtime.task_contract import require_task_agent_binding
from local_agent.supervisor import resources as resource_admission
from local_agent.supervisor import task_dedupe
from local_agent.supervisor.macos_release_admission import (
    MacReleaseAdmissionBlocked, macos_task_admission,
)

WORKER_RESOURCE_BUSY = 13
WORKER_MACHINE_BUSY = 14
PARALLEL_DAEMON_VERSION = serial_worker.MULTIREPO_DAEMON_VERSION
MachineResourceBusy = resource_admission.MachineResourceBusy


def resource_lock_dir() -> Path:
    return resource_admission.resource_lock_dir(agentd.STATE_DIR)


def task_resources(task: dict[str, object]) -> tuple[str, ...]:
    """Return the explicit validated external resource contract for one task."""
    return resource_admission.task_resources(task)


@contextlib.contextmanager
def machine_resource_lease(task: dict[str, object]) -> Iterator[tuple[str, ...]]:
    """Acquire external machine resources without waiting after task selection."""
    with resource_admission.machine_resource_lease(
        task,
        lock_dir=resource_lock_dir(),
        command_env=core.ENV,
    ) as resources:
        yield resources


def _waiting_status_context(task_id: str, resource: str) -> tuple[str, bool]:
    """Preserve the first wait timestamp and detect a materially changed wait."""
    try:
        payload = json.loads(agentd.LOCAL_STATUS_PATH.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError, AttributeError):
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    same_wait = (
        payload.get("state") == "waiting_resource"
        and payload.get("pending_task_id") == task_id
        and payload.get("blocked_resources") == [resource]
    )
    waiting_since = payload.get("waiting_since") if same_wait else None
    if not isinstance(waiting_since, str) or not waiting_since:
        waiting_since = agentd.now_iso()
    return waiting_since, not same_wait


def _reject_task_binding(repository: RepositoryContext, task: dict[str, object]) -> None:
    task_id = str(task.get("id", ""))
    expected = repository.agent_binding
    provided = task.get("agent_binding")
    failure_reason = "agent_binding_missing" if provided is None else "agent_binding_mismatch"
    result = {
        "id": task_id,
        "status": "failed",
        "failure_reason": failure_reason,
        "task_digest": agentd.task_digest(task),
        "started_at": None,
        "finished_at": agentd.now_iso(),
        "daemon_version": PARALLEL_DAEMON_VERSION,
        "repository_id": repository.repository_id,
        "repository": repository.repository,
        "expected_agent_binding": expected,
        "provided_agent_binding": provided,
        "error": (
            "Task rejected before claim because its agent_binding does not match "
            "the bound repository."
        ),
    }
    core.publish_result(task_id, result)
    agentd.publish_run_state(
        task_id,
        {
            "event": "task_rejected",
            "status": "failed",
            "failure_reason": failure_reason,
            "expected_agent_binding": expected,
            "provided_agent_binding": provided,
            "updated_at": agentd.now_iso(),
        },
        force_remote=True,
    )
    core.log(
        f"agent binding rejected repository={repository.repository_id} task={task_id} "
        f"expected={expected} provided={provided}"
    )


def _filter_pending_task_bindings(
    repository: RepositoryContext,
    pending: list[task_dedupe.PendingTask],
) -> list[task_dedupe.PendingTask]:
    """Reject stale/misbound work before it can affect repository-local dedupe intent."""
    assert repository.agent_binding is not None
    accepted: list[task_dedupe.PendingTask] = []
    for item in pending:
        _, task = item
        try:
            require_task_agent_binding(task, repository.agent_binding)
        except ValueError:
            _reject_task_binding(repository, task)
            continue
        try:
            check_host_task_admission(
                repository, task, item[0],
                operator_disabled=agent_operator.is_disabled(),
            )
        except HostAdmissionUnavailable:
            # Pending work must not enter dedupe before operator proof.
            raise
        except HostAdmissionBlocked as exc:
            serial_worker.reject_host_task(repository, task, exc.reason)
            continue
        accepted.append(item)
    return accepted


def _publish_duplicate_suppression(
    repository: RepositoryContext,
    suppressed: task_dedupe.SuppressedTask,
) -> None:
    _, task = suppressed.item
    task_id = str(task.get("id", ""))
    result = {
        "id": task_id,
        "status": "done",
        "outcome": "duplicate_task_suppressed",
        "task_digest": agentd.task_digest(task),
        "execution_fingerprint": suppressed.execution_fingerprint,
        "dedupe_queue_key": suppressed.queue_key,
        "duplicate_of": suppressed.duplicate_of,
        "duplicate_reason": suppressed.reason,
        "started_at": None,
        "finished_at": agentd.now_iso(),
        "daemon_version": PARALLEL_DAEMON_VERSION,
        "repository_id": repository.repository_id,
        "repository": repository.repository,
        "message": (
            "Equivalent work is already queued, admitted, or was completed recently; "
            "duplicate execution was intentionally suppressed."
        ),
    }
    core.publish_result(task_id, result)
    agentd.publish_run_state(
        task_id,
        {
            "event": "duplicate_task_suppressed",
            "status": "done",
            "outcome": "duplicate_task_suppressed",
            "execution_fingerprint": suppressed.execution_fingerprint,
            "dedupe_queue_key": suppressed.queue_key,
            "duplicate_of": suppressed.duplicate_of,
            "duplicate_reason": suppressed.reason,
            "updated_at": agentd.now_iso(),
        },
        force_remote=True,
    )
    core.log(
        f"duplicate task suppressed repository={repository.repository_id} "
        f"task={task_id} duplicate_of={suppressed.duplicate_of} "
        f"reason={suppressed.reason}"
    )


def _publish_invalid_dedupe_task(
    repository: RepositoryContext,
    invalid: task_dedupe.InvalidTask,
) -> None:
    _, task = invalid.item
    task_id = str(task.get("id", ""))
    result = {
        "id": task_id,
        "status": "failed",
        "failure_reason": invalid.reason,
        "task_digest": agentd.task_digest(task),
        "started_at": None,
        "finished_at": agentd.now_iso(),
        "daemon_version": PARALLEL_DAEMON_VERSION,
        "repository_id": repository.repository_id,
        "repository": repository.repository,
        "error": invalid.error,
    }
    core.publish_result(task_id, result)
    agentd.publish_run_state(
        task_id,
        {
            "event": "task_rejected",
            "status": "failed",
            "failure_reason": invalid.reason,
            "error": invalid.error,
            "updated_at": agentd.now_iso(),
        },
        force_remote=True,
    )
    core.log(
        f"dedupe task rejected reason={invalid.reason} repository={repository.repository_id} "
        f"task={task_id}: {invalid.error}"
    )


def _coalesce_pending_tasks(
    repository: RepositoryContext,
    pending: list[task_dedupe.PendingTask],
) -> list[task_dedupe.PendingTask]:
    """Drain invalid and duplicate queue entries before selecting execution work."""
    plan = task_dedupe.plan_pending(
        serial_worker.repository_state_dir(repository),
        pending,
    )
    for invalid in plan.invalid:
        _publish_invalid_dedupe_task(repository, invalid)
    for suppressed in plan.suppressed:
        _publish_duplicate_suppression(repository, suppressed)
    return list(plan.candidates)


def _repository_binding_ready(repository: RepositoryContext) -> bool:
    if repository.agent_binding is None:
        serial_worker.publish_repository_status(
            repository,
            "unbound",
            force_remote=True,
            execution_variant="parallel",
            error="repository registry is missing agent_binding",
        )
        core.log(
            "repository admission blocked: missing agent_binding "
            f"repository={repository.repository_id}"
        )
        return False
    try:
        validate_repository_control_binding(
            repository_id=repository.repository_id,
            repository=repository.repository,
            expected_agent_binding=repository.agent_binding,
            control_dir=repository.control,
        )
    except ValueError as exc:
        serial_worker.publish_repository_status(
            repository,
            "binding_error",
            force_remote=True,
            execution_variant="parallel",
            error=str(exc),
        )
        core.log(
            "repository admission blocked by binding check "
            f"repository={repository.repository_id}: {exc}"
        )
        return False
    return True


def poll_repository_once(repository: RepositoryContext) -> bool:
    """Hold the optional Mac release lease throughout the parallel project turn."""
    with macos_task_admission(agentd.STATE_DIR):
        return _poll_repository_once_admitted(repository)


def _poll_repository_once_admitted(repository: RepositoryContext) -> bool:
    """Poll one repository and execute at most one resource-arbitrated task."""
    serial_worker.bind_repository(repository)
    serial_worker.validate_repository_checkouts(repository)
    previous_version = agentd.DAEMON_VERSION
    agentd.DAEMON_VERSION = PARALLEL_DAEMON_VERSION
    try:
        serial_worker.sync_control_quietly()
        if agent_operator.is_disabled():
            serial_worker.publish_repository_status(
                repository,
                "disabled",
                force_remote=True,
                execution_variant="parallel",
            )
            return False
        if not _repository_binding_ready(repository):
            return False
        agentd.recover_stale_claims()
        agentd.recover_invalid_task_files()
        serial_worker.handle_repository_control(repository)
        if agent_operator.is_disabled():
            serial_worker.publish_repository_status(
                repository,
                "disabled",
                force_remote=True,
                execution_variant="parallel",
            )
            return False
        try:
            pending = _filter_pending_task_bindings(repository, agentd.pending_tasks())
        except HostAdmissionUnavailable:
            serial_worker.publish_repository_status(
                repository, "host_admission_waiting", force_remote=False,
                execution_variant="parallel",
            )
            return False
        pending = _coalesce_pending_tasks(repository, pending)
        if not pending:
            state = "publication_pending" if agentd.has_pending_publications() else "idle"
            serial_worker.publish_repository_status(
                repository,
                state,
                force_remote=False,
                execution_variant="parallel",
            )
            return False

        _, task = pending[0]
        task_id = str(task.get("id", ""))
        try:
            assert repository.agent_binding is not None
            require_task_agent_binding(task, repository.agent_binding)
        except ValueError:
            _reject_task_binding(repository, task)
            return True

        # Recheck after selection, before dedupe admission and resource claims.
        try:
            check_host_task_admission(
                repository, task, pending[0][0],
                operator_disabled=agent_operator.is_disabled(),
            )
        except HostAdmissionUnavailable:
            serial_worker.publish_repository_status(
                repository, "host_admission_waiting", force_remote=False,
                execution_variant="parallel",
            )
            return False
        except HostAdmissionBlocked as exc:
            serial_worker.reject_host_task(repository, task, exc.reason)
            return True

        dedupe_state_dir = serial_worker.repository_state_dir(repository)
        task_dedupe.record_admission(dedupe_state_dir, task)
        try:
            with machine_resource_lease(task) as resources:
                serial_worker.publish_repository_status(
                    repository,
                    "running",
                    force_remote=True,
                    current_task_id=task_id,
                    active_resources=list(resources),
                    execution_variant="parallel",
                )
                core.log(
                    f"[parallel] TASK START repository={repository.repository_id} "
                    f"task={task_id} binding={repository.agent_binding} "
                    f"resources={list(resources)}"
                )
                from local_agent.foundation.attached_checkout import execute_in_task_workspace

                with serial_worker.ActiveRepositoryControlWatcher(repository, task_id):
                    outcome = execute_in_task_workspace(
                        repository, core, task, agentd.execute_task,
                    )
        except MachineResourceBusy as exc:
            waiting_since, force_remote = _waiting_status_context(task_id, exc.resource)
            serial_worker.publish_repository_status(
                repository,
                "waiting_resource",
                force_remote=force_remote,
                current_task_id=None,
                pending_task_id=task_id,
                blocked_resources=[exc.resource],
                waiting_since=waiting_since,
                retrying=True,
                execution_variant="parallel",
            )
            raise

        task_dedupe.record_completion(dedupe_state_dir, task, outcome)
        state = "publication_pending" if outcome == "publication_pending" else "idle"
        serial_worker.publish_repository_status(
            repository,
            state,
            force_remote=True,
            last_task_id=task_id,
            execution_variant="parallel",
        )
        return True
    finally:
        agentd.DAEMON_VERSION = previous_version


def main() -> int:
    serial_worker.install_signal_handlers()
    args = serial_worker.parse_args()
    try:
        repository = serial_worker.repository_by_id(
            args.repository_id,
            registry_path=args.registry,
            expected_config_digest=args.expected_config_digest,
        )
    except ValueError as exc:
        core.log(str(exc))
        return serial_worker.WORKER_CONFIG_CHANGED

    try:
        with serial_worker.repository_execution_lease(repository):
            processed = poll_repository_once(repository)
    except MacReleaseAdmissionBlocked as exc:
        core.log(f"parallel repository turn deferred by Mac release fence: {exc}")
        return serial_worker.WORKER_BUSY
    except ExecutionLeaseBusy as exc:
        core.log(
            f"repository execution lease busy repository={repository.repository_id} "
            f"key={exc.key}"
        )
        return serial_worker.WORKER_BUSY
    except MachineResourceBusy as exc:
        core.log(
            f"machine resource busy repository={repository.repository_id} "
            f"resource={exc.resource}"
        )
        if exc.resource == "machine":
            return WORKER_MACHINE_BUSY
        return WORKER_RESOURCE_BUSY

    return serial_worker.WORKER_PROCESSED if processed else serial_worker.WORKER_IDLE


if __name__ == "__main__":
    raise SystemExit(main())
