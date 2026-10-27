#!/usr/bin/env python3
from __future__ import annotations

import argparse
import contextlib
import fcntl
import json
import os
import signal
import subprocess
import sys
import time
from collections.abc import Iterator
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any

from local_agent.paths import repository_root

from local_agent.supervisor import control as supervisor_control
from local_agent.supervisor import host_control as host_supervisor_control
from local_agent.supervisor import host_remote_worker
from local_agent.supervisor import conversation as conversation_supervisor
from local_agent.supervisor import observability as operator_observability
from local_agent.supervisor import policy as supervisor_policy
from local_agent.supervisor import scheduling
from local_agent.supervisor.polling import LivePollingPreferences
from local_agent.supervisor.macos_release_admission import (
    MacReleaseAdmissionBlocked,
    macos_task_admission,
)
import local_agent.operator.local as agent_operator
import local_agent.repository.worker as serial_worker
import local_agent.daemon.service as agentd
from local_agent.supervisor.worker import (
    PARALLEL_DAEMON_VERSION,
    WORKER_MACHINE_BUSY,
    WORKER_RESOURCE_BUSY,
)
from local_agent.foundation.process import (
    LEASE_FDS_ENV,
    LEASE_KEYS_DIGEST_ENV,
    RESOURCE_LEASE_FDS_ENV,
    ExecutionLeaseBusy,
    acquire_execution_leases,
    defer_termination,
    popen_registered,
    process_shutdown_requested,
    terminate_active_processes,
    terminate_process_group,
    unregister_process,
)
from local_agent.repository.context import (
    RepositoryContext,
    load_repository_registry,
    repository_config_digest,
    repository_lease_keys,
)

def control_is_host_only(repositories: list[RepositoryContext]) -> bool:
    """Whether a legacy project uses host-addressed task transport."""
    return bool(repositories and repositories[0].host_mailbox_enabled)


def machine_control_is_host_owned(
    repository: RepositoryContext | host_supervisor_control.HostControlSource,
) -> bool:
    """Dedicated host control must not run legacy project-control services."""
    return (
        isinstance(repository, host_supervisor_control.HostControlSource)
        or repository.host_mailbox_enabled
    )


REAP_INTERVAL_SECONDS = 0.25
DISABLED_POLL_SECONDS = 0.5
REPOSITORY_RETRY_SECONDS = 1.0
ERROR_RETRY_SECONDS = 1.0
MAX_ONCE_DEFERRALS = 120
LOCAL_LOG_MAX_BYTES = 2 * 1024 * 1024
LOCAL_LOG_KEEP_BYTES = 1024 * 1024
LOCAL_STDOUT_LOG_PATH = Path.home() / "Library" / "Logs" / "local-agent.log"
LOCAL_STDERR_LOG_PATH = Path.home() / "Library" / "Logs" / "local-agent-error.log"
PARALLEL_EXECUTION_MODEL = "parallel_repository_supervisor"
_daemon_lock_handle: Any | None = None


class ControlProbeResult(Enum):
    CLEAR = "clear"
    PENDING = "pending"
    LEASE_BUSY = "lease_busy"
    DEFERRED = "deferred"


@dataclass
class RunningWorker:
    repository_id: str
    proc: subprocess.Popen[str]
    started_at: float


def log(message: str) -> None:
    agentd.log(f"[parallel] {message}")


def supervisor_status_fields(
    control_repository: RepositoryContext,
    max_workers: int,
) -> dict[str, Any]:
    return {
        "execution_model": PARALLEL_EXECUTION_MODEL,
        "supervisor_pid": os.getpid(),
        "max_parallel_workers": max_workers,
        "supervisor_control_repository": control_repository.repository_id,
    }


def compact_inherited_log_file(
    fd: int,
    path: Path,
    *,
    max_bytes: int = LOCAL_LOG_MAX_BYTES,
    keep_bytes: int = LOCAL_LOG_KEEP_BYTES,
) -> bool:
    """Keep only recent text when an inherited append log exceeds its bound."""
    if max_bytes <= 0 or keep_bytes <= 0 or keep_bytes >= max_bytes:
        raise ValueError("log bounds require 0 < keep_bytes < max_bytes")
    try:
        fd_stat = os.fstat(fd)
        path_stat = path.stat()
    except OSError:
        return False
    if (fd_stat.st_dev, fd_stat.st_ino) != (path_stat.st_dev, path_stat.st_ino):
        return False
    if path_stat.st_size <= max_bytes:
        return False

    try:
        flags = fcntl.fcntl(fd, fcntl.F_GETFL)
        if not flags & os.O_APPEND:
            fcntl.fcntl(fd, fcntl.F_SETFL, flags | os.O_APPEND)

        stream = sys.stdout if fd == 1 else sys.stderr if fd == 2 else None
        if stream is not None:
            stream.flush()

        desired_start = max(0, path_stat.st_size - keep_bytes)
        read_start = max(0, desired_start - 4096)
        with path.open("rb") as handle:
            handle.seek(read_start)
            tail = handle.read()
        relative_start = desired_start - read_start
        if read_start > 0:
            newline = tail.find(bytes((10,)), relative_start)
            tail = tail[newline + 1 :] if newline >= 0 else tail[relative_start:]
        else:
            tail = tail[relative_start:]
        if len(tail) > keep_bytes:
            tail = tail[-keep_bytes:]
        tail = tail.decode("utf-8", errors="ignore").encode("utf-8")

        os.ftruncate(fd, 0)
        os.lseek(fd, 0, os.SEEK_SET)
        view = memoryview(tail)
        while view:
            written = os.write(fd, view)
            if written <= 0:
                raise OSError("short write while compacting local log")
            view = view[written:]
        return True
    except (OSError, ValueError):
        return False


def maintain_local_log_files() -> tuple[str, ...]:
    compacted: list[str] = []
    for fd, path in ((1, LOCAL_STDOUT_LOG_PATH), (2, LOCAL_STDERR_LOG_PATH)):
        if compact_inherited_log_file(fd, path):
            compacted.append(path.name)
    return tuple(compacted)


def worker_command(
    repository: RepositoryContext,
    *,
    registry_path: Path | None,
) -> list[str]:
    command = [
        sys.executable,
        "-m",
        "local_agent.supervisor.worker",
        "--repository-id",
        repository.repository_id,
        "--expected-config-digest",
        repository_config_digest(repository),
    ]
    if registry_path is not None:
        command.extend(["--registry", str(registry_path)])
    return command


def start_worker(
    repository: RepositoryContext,
    *,
    registry_path: Path | None,
    running: dict[str, RunningWorker],
    max_workers: int,
) -> bool:
    """Prevent new Mac worker launches while an authorized release fence is held."""
    try:
        with macos_task_admission(agentd.STATE_DIR):
            return _start_worker_admitted(
                repository, registry_path=registry_path,
                running=running, max_workers=max_workers,
            )
    except MacReleaseAdmissionBlocked:
        return False


def _start_worker_admitted(
    repository: RepositoryContext,
    *,
    registry_path: Path | None,
    running: dict[str, RunningWorker],
    max_workers: int,
) -> bool:
    if agent_operator.is_disabled():
        return False
    try:
        publish_local_supervisor_status(running, max_workers=max_workers, state="running")
        with serial_worker.repository_execution_lease(repository):
            env = os.environ.copy()
            env["LOCAL_AGENT_SUPERVISOR_PID"] = str(os.getpid())
            proc = popen_registered(
                worker_command(repository, registry_path=registry_path),
                cwd=repository_root(),
                env=env,
                text=True,
                start_new_session=True,
            )
            setattr(proc, "_local_agent_process_group", proc.pid)
            running[repository.repository_id] = RunningWorker(
                repository_id=repository.repository_id,
                proc=proc,
                started_at=time.monotonic(),
            )
            return True
    except ExecutionLeaseBusy:
        return False
    except OSError as exc:
        log(
            f"repository worker spawn failed repository={repository.repository_id}: "
            f"{type(exc).__name__}: {exc}"
        )
        return False
    finally:
        # Register successful dispatch before exposing its state. Failed admission
        # restores the current worker set after releasing any acquired leases.
        publish_local_supervisor_status(running, max_workers=max_workers)


def start_host_task_worker(
    source: host_supervisor_control.HostControlSource,
    task_id: str,
    *,
    running: dict[str, RunningWorker],
    max_workers: int,
) -> bool:
    """Defer Mac host worker spawning during a frozen release admission."""
    try:
        with macos_task_admission(agentd.STATE_DIR):
            return _start_host_task_worker_admitted(
                source, task_id, running=running, max_workers=max_workers,
            )
    except MacReleaseAdmissionBlocked:
        return False


def _start_host_task_worker_admitted(
    source: host_supervisor_control.HostControlSource,
    task_id: str,
    *,
    running: dict[str, RunningWorker],
    max_workers: int,
) -> bool:
    """Spawn an opt-in remote host worker within the existing supervisor pool."""
    slot_id = f"@host:{task_id}"
    if agent_operator.is_disabled() or slot_id in running or len(running) >= max_workers:
        return False
    try:
        publish_local_supervisor_status(running, max_workers=max_workers, state="running")
        command = host_remote_worker.worker_command(
            task_id, state_dir=agentd.STATE_DIR,
            production_checkout=agentd.SELF_REPO,
        )
        env = os.environ.copy()
        env["LOCAL_AGENT_SUPERVISOR_PID"] = str(os.getpid())
        proc = popen_registered(
            command, cwd=repository_root(), env=env, text=True,
            start_new_session=True,
        )
        setattr(proc, "_local_agent_process_group", proc.pid)
        running[slot_id] = RunningWorker(
            repository_id=slot_id, proc=proc, started_at=time.monotonic(),
        )
        return True
    except OSError as exc:
        log(f"host task worker spawn failed: {type(exc).__name__}: {exc}")
        return False
    finally:
        publish_local_supervisor_status(running, max_workers=max_workers)


def reap_workers(
    running: dict[str, RunningWorker],
    schedules: dict[str, scheduling.RepositorySchedule],
) -> dict[str, int]:
    completed: dict[str, int] = {}
    now = time.monotonic()
    worker_limit = agentd.TIMEOUTS.task_max + supervisor_policy.WORKER_TURN_GRACE_SECONDS
    for repository_id, slot in list(running.items()):
        return_code = slot.proc.poll()
        if return_code is None and now - slot.started_at > worker_limit:
            log(
                f"repository worker timed out repository={repository_id} "
                f"limit={worker_limit}s"
            )
            terminate_process_group(slot.proc, log)
            return_code = slot.proc.poll()
            if return_code is None:
                continue

        if return_code is None:
            continue

        unregister_process(slot.proc)
        del running[repository_id]
        completed[repository_id] = return_code
        schedule = schedules.setdefault(repository_id, scheduling.RepositorySchedule())
        completed_at = time.monotonic()
        host_deferred = repository_id.startswith("@host:") and return_code in {
            host_remote_worker.HOST_WORKER_RESOURCE_DEFERRED,
            host_remote_worker.HOST_WORKER_CONTROL_DEFERRED,
        }
        control_deferred = host_deferred and return_code == host_remote_worker.HOST_WORKER_CONTROL_DEFERRED
        normal = {
            serial_worker.WORKER_PROCESSED,
            WORKER_MACHINE_BUSY,
            WORKER_RESOURCE_BUSY,
            serial_worker.WORKER_IDLE,
            serial_worker.WORKER_BUSY,
            serial_worker.WORKER_CONFIG_CHANGED,
        }
        if return_code in normal or host_deferred:
            scheduling.reset_worker_failure_state(schedule)
        if return_code in {WORKER_MACHINE_BUSY, WORKER_RESOURCE_BUSY} or host_deferred:
            schedule.resource_deferrals += 1
            retry_policy = scheduling.control_defer_retry_seconds if control_deferred else scheduling.resource_retry_seconds
            retry = retry_policy(schedule.resource_deferrals)
            schedule.retry_not_before = completed_at + retry
            kind = "machine exclusion" if return_code == WORKER_MACHINE_BUSY else "named resource"
            if not host_deferred or scheduling.repeated_failure_log_due(schedule.last_deferral_log_at, completed_at):
                log(
                    f"{'host control' if control_deferred else kind} deferred repository={repository_id} "
                    f"attempt={schedule.resource_deferrals} retry_in={retry:.0f}s"
                )
                schedule.last_deferral_log_at = completed_at
            continue
        scheduling.reset_resource_deferral_state(schedule)
        if return_code == serial_worker.WORKER_PROCESSED:
            schedule.last_activity_at = completed_at
            schedule.retry_not_before = 0.0
            log(f"TASK DONE repository={repository_id}")
        elif return_code == serial_worker.WORKER_IDLE:
            schedule.retry_not_before = 0.0
        elif return_code == serial_worker.WORKER_BUSY:
            schedule.retry_not_before = completed_at + REPOSITORY_RETRY_SECONDS
            log(f"repository turn deferred repository={repository_id}: lease busy")
        elif return_code == serial_worker.WORKER_CONFIG_CHANGED:
            schedule.retry_not_before = completed_at + REPOSITORY_RETRY_SECONDS
            log(f"repository turn deferred repository={repository_id}: config changed")
        else:
            if schedule.last_failure_code == return_code:
                schedule.consecutive_failures += 1
            else:
                schedule.consecutive_failures = 1
                schedule.last_failure_code = return_code
                schedule.last_failure_log_at = None
            retry = scheduling.worker_failure_retry_seconds(schedule.consecutive_failures)
            schedule.retry_not_before = completed_at + retry
            if scheduling.repeated_failure_log_due(schedule.last_failure_log_at, completed_at):
                log(
                    f"repository worker failed repository={repository_id} "
                    f"exit={return_code} consecutive={schedule.consecutive_failures} "
                    f"retry_in={retry:.0f}s"
                )
                schedule.last_failure_log_at = completed_at
    return completed


def record_once_deferral(
    counts: dict[str, int],
    failed: set[str],
    repository_id: str,
    *,
    limit: int = MAX_ONCE_DEFERRALS,
) -> int:
    """Count a one-shot deferral and make bounded failure terminal."""
    count = counts.get(repository_id, 0) + 1
    counts[repository_id] = count
    if count >= limit:
        failed.add(repository_id)
    return count


def _restore_env_value(target: dict[str, str], name: str, previous: str | None) -> None:
    if previous is None:
        target.pop(name, None)
    else:
        target[name] = previous


@contextlib.contextmanager
def supervisor_control_leases(
    repositories: list[RepositoryContext],
    *,
    control_source: host_supervisor_control.HostControlSource | None = None,
) -> Iterator[None]:
    """Hold project identities for global effects, plus independent host control."""
    if not repositories and control_source is None:
        raise ValueError("supervisor control requires a trusted source")
    keys = tuple(
        key
        for repository in repositories
        for key in repository_lease_keys(repository)
    )
    if control_source is not None:
        keys += host_supervisor_control.host_control_lease_keys(control_source)
    leases = acquire_execution_leases(serial_worker.repository_lease_dir(), keys)
    updates = leases.environment()
    previous_os = {name: os.environ.get(name) for name in updates}
    previous_core = {name: agentd.core.ENV.get(name) for name in updates}
    os.environ.update(updates)
    agentd.core.ENV.update(updates)
    try:
        yield
    finally:
        for target, previous in (
            (os.environ, previous_os),
            (agentd.core.ENV, previous_core),
        ):
            for name, value in previous.items():
                _restore_env_value(target, name, value)
        leases.close()


def restart_parallel_supervisor(
    reason: str,
    *,
    registry_path: Path | None,
    max_workers: int,
    once: bool,
) -> None:
    agentd.publish_daemon_status("restarting", force_remote=True, reason=reason)
    log(f"restarting parallel supervisor: {reason}")
    for name in (LEASE_FDS_ENV, LEASE_KEYS_DIGEST_ENV, RESOURCE_LEASE_FDS_ENV):
        os.environ.pop(name, None)
        agentd.core.ENV.pop(name, None)

    command = [sys.executable, str(repository_root() / "agent_parallel.py")]
    if registry_path is not None:
        command.extend(["--registry", str(registry_path)])
    command.extend(["--max-workers", str(max_workers)])
    if once:
        command.append("--once")
    try:
        os.execv(command[0], command)
    except OSError as exc:
        log(f"exec restart failed; asking launchd to restart: {exc}")
        raise SystemExit(75) from exc


@contextlib.contextmanager
def route_parallel_restarts(
    *,
    registry_path: Path | None,
    max_workers: int,
    once: bool,
) -> Iterator[None]:
    original_restart = agentd.restart_self

    def routed_restart(reason: str) -> None:
        restart_parallel_supervisor(
            reason,
            registry_path=registry_path,
            max_workers=max_workers,
            once=once,
        )

    agentd.restart_self = routed_restart
    try:
        yield
    finally:
        agentd.restart_self = original_restart


def bound_control_request_from_checkout(
    source: host_supervisor_control.HostControlSource | None = None,
) -> dict[str, Any] | None:
    path = agentd.core.CONTROL / agentd.REMOTE_CONTROL_REQUEST
    if not path.exists():
        return None
    try:
        request = json.loads(path.read_text(encoding="utf-8"))
        control_id = str(request["id"])
        str(request["action"])
    except Exception as exc:
        log(f"invalid daemon control request during probe: {type(exc).__name__}: {exc}")
        return None
    if not agentd.valid_control_id(control_id):
        log(f"invalid daemon control id during probe: {control_id!r}")
        return None
    if source is not None:
        try:
            return host_supervisor_control.require_host_control_request(source, request)
        except ValueError as exc:
            log(f"rejected host control request: {exc}")
            return None
    return request if isinstance(request, dict) else None


def current_bound_request(
    source: host_supervisor_control.HostControlSource | None = None,
) -> dict[str, Any] | None:
    return (
        bound_control_request_from_checkout(source)
        if source is not None else bound_control_request_from_checkout()
    )


def handle_bound_disable_control(repository: RepositoryContext) -> bool:
    """Consume a global disable request from the bound supervisor control checkout."""
    source = repository if isinstance(repository, host_supervisor_control.HostControlSource) else None
    request = current_bound_request(source)
    if request is None or str(request.get("action", "")) != "disable":
        return False
    control_id = str(request["id"])
    try:
        if agentd.control_ack_published(control_id):
            return True
    except Exception as exc:
        log(f"disable ACK probe degraded id={control_id}: {type(exc).__name__}: {exc}")
        return True

    agent_operator.disable_agent(
        control_id=control_id,
        repository_id=repository.repository_id,
        reason="remote_control",
    )
    if isinstance(repository, host_supervisor_control.HostControlSource):
        agentd.publish_control_ack(control_id, "disable", "completed", result="agent_disabled")
    else:
        serial_worker.publish_repository_control_ack(
            repository,
            control_id,
            "disable",
            "completed",
            result="agent_disabled",
        )
    log(
        f"operator disabled Local Agent via supervisor control "
        f"repository={repository.repository_id} control={control_id}"
    )
    return True


def pending_control_request_from_bound_checkout(
    source: host_supervisor_control.HostControlSource | None = None,
) -> ControlProbeResult:
    """Classify only a target-validated request after syncing its checkout."""
    request = current_bound_request(source)
    if request is None:
        return ControlProbeResult.CLEAR
    control_id = str(request["id"])
    action = str(request.get("action", ""))
    if source is not None and action in {"host_diagnostics", "status", "restart", "self_update"}:
        # The independent service controller owns these actions. A task
        # scheduler must neither execute them nor wait for their ACK.
        return ControlProbeResult.CLEAR
    if action == "cancel_task":
        # The target repository worker owns cancellation so the supervisor must
        # not drain unrelated workers or reject the request as an unknown global action.
        return ControlProbeResult.CLEAR
    try:
        published = agentd.control_ack_published(control_id)
    except Exception as exc:
        log(f"control ACK probe degraded id={control_id}: {type(exc).__name__}: {exc}")
        return ControlProbeResult.DEFERRED
    return ControlProbeResult.CLEAR if published else ControlProbeResult.PENDING


def probe_control_request(
    repository: RepositoryContext | host_supervisor_control.HostControlSource,
    *,
    repositories: list[RepositoryContext] | None = None,
    operator_campaign: conversation_supervisor.RunningOperatorCampaign | None = None,
    max_workers: int | None = None,
) -> ControlProbeResult:
    """Probe control while other repositories run without invoking global actions."""
    try:
        if isinstance(repository, host_supervisor_control.HostControlSource):
            project_paths = tuple(
                path for item in (repositories or [])
                for path in (item.control, item.work, item.checkpoints)
            )
            host_supervisor_control.validate_host_control_checkout(
                repository, production_checkout=agentd.SELF_REPO,
                project_workspaces=project_paths,
            )
        lease = (
            host_supervisor_control.host_supervisor_lease(repository)
            if isinstance(repository, host_supervisor_control.HostControlSource)
            else serial_worker.repository_execution_lease(repository)
        )
        with lease:
            supervisor_control.bind_supervisor_control(
                repository,
                daemon_version=PARALLEL_DAEMON_VERSION,
            )
            supervisor_control.sync_control_quietly()
            if handle_bound_disable_control(repository):
                return ControlProbeResult.CLEAR
            if not machine_control_is_host_owned(repository):
                conversation_supervisor.service_control_plane()
            source = repository if isinstance(repository, host_supervisor_control.HostControlSource) else None
            probe_result = (
                pending_control_request_from_bound_checkout(source)
                if source is not None else pending_control_request_from_bound_checkout()
            )
            if probe_result is not ControlProbeResult.CLEAR:
                return probe_result
            if (not machine_control_is_host_owned(repository)
                    and repositories is not None and max_workers is not None):
                try:
                    operator_observability.publish_operator_status(
                        repositories,
                        operator_campaign,
                        max_workers=max_workers,
                    )
                except Exception as exc:
                    log(
                        "operator observability publish degraded: "
                        f"{type(exc).__name__}: {exc}"
                    )
            return ControlProbeResult.CLEAR
    except ExecutionLeaseBusy:
        return ControlProbeResult.LEASE_BUSY
    except Exception as exc:
        log(
            f"supervisor control probe degraded repository={repository.repository_id}: "
            f"{type(exc).__name__}: {exc}"
        )
        return ControlProbeResult.DEFERRED


def service_control(
    repositories: list[RepositoryContext],
    *,
    registry_path: Path | None,
    max_workers: int,
    once: bool,
    operator_campaign: conversation_supervisor.RunningOperatorCampaign | None = None,
) -> bool:
    host_source = host_supervisor_control.selected_host_control_source(
        repositories, state_dir=agentd.STATE_DIR, production_checkout=agentd.SELF_REPO,
    )
    if not repositories and host_source is None:
        return False
    control_repository = host_source or repositories[0]
    status_fields = supervisor_status_fields(control_repository, max_workers)
    try:
        # Announce control activity before acquiring leases or starting Git sync.
        # The guard must never interpret those live holders as idle orphans.
        publish_local_supervisor_status(
            {},
            max_workers=max_workers,
            state="running",
            control_repository=control_repository,
        )
        leases = (
            supervisor_control_leases(repositories, control_source=host_source)
            if host_source is not None else supervisor_control_leases(repositories)
        )
        with leases:
            supervisor_control.bind_supervisor_control(
                control_repository,
                daemon_version=PARALLEL_DAEMON_VERSION,
            )
            agentd.DAEMON_VERSION = PARALLEL_DAEMON_VERSION
            supervisor_control.sync_control_quietly()
            if handle_bound_disable_control(control_repository):
                agentd.publish_daemon_status(
                    "disabled",
                    force_remote=False,
                    **status_fields,
                )
                return True
            if not machine_control_is_host_owned(control_repository):
                conversation_supervisor.service_control_plane()
            agentd.publish_daemon_status(
                "idle",
                force_remote=False,
                **status_fields,
            )
            with route_parallel_restarts(
                registry_path=registry_path,
                max_workers=max_workers,
                once=once,
            ):
                request = current_bound_request(host_source)
                if host_source is not None:
                    # Disable remains authoritative here. Other service
                    # actions are consumed outside this scheduler.
                    pass
                elif request is None or str(request.get("action", "")) != "cancel_task":
                    agentd.handle_control_request(status_extra=status_fields)
                if not machine_control_is_host_owned(control_repository):
                    agentd.maybe_self_update()
            try:
                if not machine_control_is_host_owned(control_repository):
                    operator_observability.publish_operator_status(
                        repositories, operator_campaign, max_workers=max_workers,
                    )
            except Exception as exc:
                log(
                    "operator observability publish degraded: "
                    f"{type(exc).__name__}: {exc}"
                )
            agentd.publish_daemon_status(
                "idle",
                force_remote=False,
                **status_fields,
            )
        return True
    except ExecutionLeaseBusy:
        return False
    except Exception as exc:
        log(
            f"supervisor control degraded repository={control_repository.repository_id}: "
            f"{type(exc).__name__}: {exc}"
        )
        return False
    finally:
        # Restore quiescence only after lease release, including failed syncs and
        # contention. Otherwise a stale control marker can hide a real orphan.
        publish_local_supervisor_status(
            {},
            max_workers=max_workers,
            state="disabled" if agent_operator.is_disabled() else "idle",
        )


def service_operator_result_publication(
    repositories: list[RepositoryContext],
    *,
    max_workers: int,
) -> bool:
    host_source = host_supervisor_control.selected_host_control_source(
        repositories, state_dir=agentd.STATE_DIR, production_checkout=agentd.SELF_REPO,
    )
    if not repositories and host_source is None:
        return False
    control_repository = host_source or repositories[0]
    if machine_control_is_host_owned(control_repository):
        return True  # No legacy conversation result publication on machine-owned control.
    try:
        publish_local_supervisor_status(
            {},
            max_workers=max_workers,
            state="running",
            control_repository=control_repository,
        )
        leases = (
            supervisor_control_leases(repositories, control_source=host_source)
            if host_source is not None else supervisor_control_leases(repositories)
        )
        with leases:
            supervisor_control.bind_supervisor_control(
                control_repository,
                daemon_version=PARALLEL_DAEMON_VERSION,
            )
            agentd.DAEMON_VERSION = PARALLEL_DAEMON_VERSION
            supervisor_control.sync_control_quietly()
            conversation_supervisor.publish_pending_result_only()
            try:
                operator_observability.publish_operator_status(
                    repositories,
                    None,
                    max_workers=max_workers,
                    force=True,
                )
            except Exception as exc:
                log(
                    "operator observability publication-closeout degraded: "
                    f"{type(exc).__name__}: {exc}"
                )
        return True
    except ExecutionLeaseBusy:
        return False
    except Exception as exc:
        log(
            "conversation operator result publication degraded "
            f"repository={control_repository.repository_id}: "
            f"{type(exc).__name__}: {exc}"
        )
        return False
    finally:
        publish_local_supervisor_status(
            {},
            max_workers=max_workers,
            state="disabled" if agent_operator.is_disabled() else "idle",
        )


def publish_local_supervisor_status(
    running: dict[str, RunningWorker],
    *,
    max_workers: int,
    state: str | None = None,
    control_repository: RepositoryContext | None = None,
) -> None:
    resolved_state = state or ("running" if running else "idle")
    control_fields = (
        {"supervisor_control_repository": control_repository.repository_id}
        if control_repository is not None
        else {}
    )
    payload = agentd.daemon_status_payload(
        resolved_state,
        execution_model=PARALLEL_EXECUTION_MODEL,
        supervisor_pid=os.getpid(),
        max_parallel_workers=max_workers,
        active_workers_observed=True,
        active_worker_count=len(running),
        active_repository_ids=sorted(
            key for key in running if not key.startswith("@host:")
        ),
        active_host_task_ids=sorted(
            key.removeprefix("@host:") for key in running if key.startswith("@host:")
        ),
        **control_fields,
    )
    agentd.atomic_write_json(agentd.LOCAL_STATUS_PATH, payload)


def shutdown_handler(signum: int, _frame: Any) -> None:
    if defer_termination(signum):
        return
    log(f"received signal {signum}; terminating active processes")
    terminate_active_processes(log)
    raise SystemExit(128 + signum)


def install_signal_handlers() -> None:
    signal.signal(signal.SIGTERM, shutdown_handler)
    signal.signal(signal.SIGINT, shutdown_handler)


def quiesce_deferred_shutdown() -> bool:
    """Stop scheduler-side spawning while another thread settles deferred SIGTERM."""
    if not process_shutdown_requested():
        return False
    time.sleep(REAP_INTERVAL_SECONDS)
    return True


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Bounded parallel multi-repository local-agent supervisor."
    )
    parser.add_argument("--registry", type=Path)
    parser.add_argument("--max-workers", type=int)
    parser.add_argument("--once", action="store_true")
    return parser.parse_args()


def main() -> int:
    global _daemon_lock_handle
    args = parse_args()
    try:
        max_workers = scheduling.resolve_max_workers(args.max_workers)
    except ValueError as exc:
        log(str(exc))
        return 2

    if args.once:
        # A single-pass caller must return a bounded, explicit refusal when
        # Mac release enrollment is absent or frozen. Otherwise --once keeps
        # retrying an intake that can never accept work and looks hung.
        try:
            with macos_task_admission(agentd.STATE_DIR):
                pass
        except MacReleaseAdmissionBlocked as exc:
            log(f"one-pass task admission blocked by Mac release policy: {exc}")
            return 75

    _daemon_lock_handle = agentd.acquire_daemon_lock()
    install_signal_handlers()

    running: dict[str, RunningWorker] = {}
    operator_campaign: conversation_supervisor.RunningOperatorCampaign | None = None
    schedules: dict[str, scheduling.RepositorySchedule] = {}
    control_deferrals = scheduling.ControlDeferralState()
    last_repository: str | None = None
    last_control_at: float | None = None
    control_pending = True
    startup_control_pending = True
    priority_repository: str | None = None
    once_completed: set[str] = set()
    once_failed: set[str] = set()
    once_deferrals: dict[str, int] = {}
    last_idle_log_at: float | None = None
    last_log_maintenance_at: float | None = None
    last_host_task_poll_at: float | None = None
    polling = LivePollingPreferences(agentd.STATE_DIR, log)
    disabled_logged = False

    try:
        host_enabled = host_supervisor_control.load_host_control_source(
            agentd.STATE_DIR / "supervisor-control.json",
        ) is not None
        repositories = load_repository_registry(path=args.registry, allow_empty=host_enabled)
        current_control = (
            host_supervisor_control.selected_host_control_source(
                repositories, state_dir=agentd.STATE_DIR,
                production_checkout=agentd.SELF_REPO,
            ) if host_enabled else None
        )
    except Exception as exc:
        log(f"initial control/registry load failed: {type(exc).__name__}: {exc}")
        return 2
    if not repositories and current_control is None:
        log("repository registry is empty and no trusted host control source is enabled")
        return 2
    control_repository = current_control or repositories[0]
    # Cache verified Git source rather than refetching every scheduler tick.
    last_verified_host_source = current_control
    last_verified_project_paths = tuple(
        path for item in repositories
        for path in (item.control, item.work, item.checkpoints)
    )
    log(
        f"parallel supervisor {PARALLEL_DAEMON_VERSION} starting "
        f"max_workers={max_workers} safe_fallback=agent_multirepo.py "
        f"control_repository={control_repository.repository_id}"
    )

    def note_once_deferral(repository_id: str) -> None:
        if not args.once:
            return
        count = record_once_deferral(
            once_deferrals,
            once_failed,
            repository_id,
        )
        if count >= MAX_ONCE_DEFERRALS:
            log(
                f"one-shot deferral limit exceeded repository={repository_id} "
                f"attempts={count}"
            )

    def note_control_deferred(
        message: str,
        *,
        emit_log: bool = True,
        lease_busy: bool = False,
    ) -> float:
        now = time.monotonic()
        retry = scheduling.record_control_deferral(
            control_deferrals,
            now=now,
            lease_busy=lease_busy,
        )
        if emit_log and scheduling.repeated_failure_log_due(
            control_deferrals.last_log_at,
            now,
        ):
            log(
                f"{message}; consecutive={control_deferrals.consecutive_deferrals} "
                f"retry_in={retry:.0f}s"
            )
            control_deferrals.last_log_at = now
        return retry

    def record_once_outcomes(completed: dict[str, int]) -> None:
        if not args.once:
            return
        deferrable = {
            WORKER_MACHINE_BUSY,
            WORKER_RESOURCE_BUSY,
            serial_worker.WORKER_BUSY,
            serial_worker.WORKER_CONFIG_CHANGED,
        }
        for repository_id, return_code in completed.items():
            if return_code in {
                serial_worker.WORKER_PROCESSED,
                serial_worker.WORKER_IDLE,
            }:
                once_completed.add(repository_id)
                once_deferrals.pop(repository_id, None)
                continue
            if return_code in deferrable:
                note_once_deferral(repository_id)
                continue
            once_failed.add(repository_id)

    host_only = machine_control_is_host_owned(control_repository)
    while True:
        try:
            if quiesce_deferred_shutdown():
                continue
            completed = reap_workers(running, schedules)
            polling.refresh(completed, serial_worker.WORKER_PROCESSED,
                            host_remote_worker.HOST_WORKER_PUBLISHED, time.monotonic())
            previous_operator_campaign = operator_campaign
            operator_campaign = conversation_supervisor.reap(
                operator_campaign,
                interrupted=agent_operator.is_disabled(),
            )
            record_once_outcomes(completed)
            if previous_operator_campaign is not None and operator_campaign is None:
                publish_local_supervisor_status(
                    running,
                    max_workers=max_workers,
                )
            if any(
                return_code == serial_worker.WORKER_PROCESSED
                for return_code in completed.values()
            ):
                last_idle_log_at = None
            if completed:
                publish_local_supervisor_status(running, max_workers=max_workers)

            if agent_operator.is_disabled():
                if not disabled_logged:
                    log("operator disable state active; stopping workers and pausing admission")
                    disabled_logged = True
                if running or operator_campaign is not None:
                    terminate_active_processes(log)
                publish_local_supervisor_status(
                    running,
                    max_workers=max_workers,
                    state="disabled",
                )
                if args.once and not running and operator_campaign is None:
                    if not host_only and conversation_supervisor.result_publish_pending():
                        if service_operator_result_publication(
                            repositories,
                            max_workers=max_workers,
                        ):
                            continue
                        time.sleep(
                            note_control_deferred(
                                "conversation operator result publication deferred while disabled"
                            )
                        )
                        continue
                    return 0
                time.sleep(REAP_INTERVAL_SECONDS if running else DISABLED_POLL_SECONDS)
                continue
            if disabled_logged:
                log("operator disable state cleared; resuming scheduler")
                disabled_logged = False
                control_pending = True
                scheduling.reset_control_deferral_state(
                    control_deferrals,
                    clear_pause=True,
                )
                last_control_at = None

            host_enabled = host_supervisor_control.load_host_control_source(
                agentd.STATE_DIR / "supervisor-control.json",
            ) is not None
            repositories = load_repository_registry(path=args.registry, allow_empty=host_enabled)
            project_paths = tuple(
                path for item in repositories
                for path in (item.control, item.work, item.checkpoints)
            )
            if host_enabled:
                configured = host_supervisor_control.load_host_control_source(
                    agentd.STATE_DIR / "supervisor-control.json",
                )
                if (configured is not None and configured == last_verified_host_source
                        and project_paths == last_verified_project_paths):
                    current_control = configured
                else:
                    current_control = host_supervisor_control.selected_host_control_source(
                        repositories, state_dir=agentd.STATE_DIR,
                        production_checkout=agentd.SELF_REPO,
                    )
                    last_verified_host_source = current_control
                    last_verified_project_paths = project_paths
            else:
                current_control = None
                last_verified_host_source = None
                last_verified_project_paths = project_paths
            if not repositories and current_control is None:
                if args.once and not running:
                    return 2
                time.sleep(ERROR_RETRY_SECONDS)
                continue
            control_repository = current_control or repositories[0]
            host_only = machine_control_is_host_owned(control_repository)
            schedules = {
                **{
                    repository.repository_id: schedules.get(
                        repository.repository_id,
                        scheduling.RepositorySchedule(),
                    )
                    for repository in repositories
                },
                **{
                    key: value for key, value in schedules.items()
                    if key.startswith("@host:")
                },
            }
            enabled = {repository.repository_id for repository in repositories}
            once_completed.intersection_update(enabled)
            once_failed.intersection_update(enabled)
            once_deferrals = {
                repository_id: count
                for repository_id, count in once_deferrals.items()
                if repository_id in enabled
            }
            control_repository_id = control_repository.repository_id
            if scheduling.bind_control_repository(
                control_deferrals,
                control_repository_id,
            ):
                last_control_at = None

            if priority_repository not in enabled:
                priority_repository = None

            for repository_id, return_code in completed.items():
                if (
                    priority_repository == repository_id
                    and return_code != WORKER_MACHINE_BUSY
                ):
                    priority_repository = None
                if (
                    return_code == WORKER_MACHINE_BUSY
                    and priority_repository is None
                    and repository_id in enabled
                    and repository_id not in once_failed
                ):
                    priority_repository = repository_id

            if args.once and priority_repository in once_failed:
                priority_repository = None

            if control_pending:
                if operator_campaign is not None:
                    probe_control_request(
                        control_repository,
                        repositories=repositories,
                        operator_campaign=operator_campaign,
                        max_workers=max_workers,
                    )
                    if agent_operator.is_disabled():
                        time.sleep(REAP_INTERVAL_SECONDS)
                        continue
                    time.sleep(REAP_INTERVAL_SECONDS)
                    continue
                if running:
                    time.sleep(REAP_INTERVAL_SECONDS)
                    continue
                if service_control(
                    repositories,
                    registry_path=args.registry,
                    max_workers=max_workers,
                    once=args.once,
                    operator_campaign=operator_campaign,
                ):
                    last_control_at = time.monotonic()
                    scheduling.reset_control_deferral_state(
                        control_deferrals,
                        clear_pause=True,
                    )
                    control_pending = False
                    startup_control_pending = False
                else:
                    retry = note_control_deferred(
                        "global control startup sync degraded; continuing unrelated task admission"
                        if startup_control_pending else "confirmed global maintenance remains pending"
                    )
                    if not startup_control_pending:
                        time.sleep(retry)
                        continue
                    # A failed synchronization is not a confirmed maintenance
                    # request. Keep per-task admission and emergency controls;
                    # retry this mailbox without holding every other lane.
                    control_pending = False
                    startup_control_pending = False

            host_intake_enabled = host_remote_worker.host_intake_polling_enabled(
                agentd.STATE_DIR, current_control)
            now = time.monotonic()
            if (
                now >= control_deferrals.retry_not_before
                and supervisor_policy.interval_due(
                    last_control_at,
                    polling.control(now, host_intake_enabled=host_intake_enabled),
                    now,
                )
            ):
                if running or operator_campaign is not None:
                    probe_result = probe_control_request(
                        control_repository,
                        repositories=repositories,
                        operator_campaign=operator_campaign,
                        max_workers=max_workers,
                    )
                    if agent_operator.is_disabled():
                        time.sleep(REAP_INTERVAL_SECONDS)
                        continue
                    if probe_result is ControlProbeResult.PENDING:
                        scheduling.reset_control_deferral_state(
                            control_deferrals,
                            clear_pause=True,
                        )
                        control_pending = True
                        log("global control request detected; draining active workers")
                        time.sleep(REAP_INTERVAL_SECONDS)
                        continue
                    if probe_result is ControlProbeResult.LEASE_BUSY:
                        note_control_deferred(
                            "global control probe deferred; control repository lease busy",
                            emit_log=False,
                            lease_busy=True,
                        )
                        action = scheduling.control_lease_busy_action(
                            control_deferrals,
                            control_repository_running=(control_repository_id in running),
                        )
                        if (
                            action
                            is scheduling.ControlLeaseBusyAction.PAUSE_CONTROL_REPOSITORY
                            and control_deferrals.paused_repository_id
                            != control_repository_id
                        ):
                            control_deferrals.paused_repository_id = control_repository_id
                            log(
                                "global control probe lease busy repeatedly; "
                                "pausing new control-repository admission "
                                f"repository={control_repository_id} "
                                "consecutive_lease_busy="
                                f"{control_deferrals.consecutive_lease_busy}"
                            )
                        elif action is scheduling.ControlLeaseBusyAction.DRAIN_ALL:
                            control_pending = True
                            log(
                                "global control probe lease busy repeatedly; "
                                "draining active workers consecutive_lease_busy="
                                f"{control_deferrals.consecutive_lease_busy}"
                            )
                            time.sleep(REAP_INTERVAL_SECONDS)
                            continue
                    elif probe_result is ControlProbeResult.DEFERRED:
                        note_control_deferred(
                            "global control probe degraded; continuing unrelated task admission"
                        )
                    else:
                        last_control_at = time.monotonic()
                        scheduling.reset_control_deferral_state(
                            control_deferrals,
                            clear_pause=True,
                        )
                else:
                    if service_control(
                        repositories,
                        registry_path=args.registry,
                        max_workers=max_workers,
                        once=args.once,
                        operator_campaign=operator_campaign,
                    ):
                        last_control_at = time.monotonic()
                        scheduling.reset_control_deferral_state(
                            control_deferrals,
                            clear_pause=True,
                        )
                    else:
                        note_control_deferred(
                            "global control service deferred; continuing task admission "
                            "after successful startup"
                        )

            if agent_operator.is_disabled():
                time.sleep(REAP_INTERVAL_SECONDS)
                continue

            if operator_campaign is None and not host_only:
                try:
                    operator_campaign = conversation_supervisor.start_if_pending()
                    if operator_campaign is not None:
                        publish_local_supervisor_status(
                            running,
                            max_workers=max_workers,
                            state="running",
                        )
                except (OSError, RuntimeError, ValueError) as exc:
                    log(
                        "conversation operator start degraded: "
                        f"{type(exc).__name__}: {exc}"
                    )

            now = time.monotonic()
            if (
                not running
                and scheduling.local_log_maintenance_due(last_log_maintenance_at, now)
            ):
                compacted_logs = maintain_local_log_files()
                last_log_maintenance_at = now
                if compacted_logs:
                    log(
                        "bounded local log history compacted "
                        f"files={','.join(compacted_logs)} keep=1MiB max=2MiB"
                    )

            now = time.monotonic()
            paused_control_repository = control_deferrals.paused_repository_id

            if priority_repository is not None:
                if (
                    priority_repository != paused_control_repository
                    and priority_repository not in running
                    and not running
                ):
                    repository = next(
                        item
                        for item in repositories
                        if item.repository_id == priority_repository
                    )
                    schedule = schedules[priority_repository]
                    if now >= schedule.retry_not_before:
                        started = start_worker(
                            repository, registry_path=args.registry,
                            running=running, max_workers=max_workers,
                        )
                        schedule.last_poll_at = time.monotonic()
                        if started:
                            last_repository = priority_repository
                        else:
                            schedule.retry_not_before = (
                                time.monotonic() + REPOSITORY_RETRY_SECONDS
                            )
                            note_once_deferral(priority_repository)
                            if args.once and priority_repository in once_failed:
                                priority_repository = None
            # Host Ops gets the next available shared worker slot before
            # ordinary project polling. Running projects are never preempted;
            # an explicit priority_repository still runs ahead of Host Ops.
            # Host intake is separate; each worker revalidates exact Git bytes.
            host_poll_now = time.monotonic()
            if (current_control is not None
                    and (last_host_task_poll_at is None
                         or host_poll_now - last_host_task_poll_at >= polling.host(host_poll_now))):
                last_host_task_poll_at = host_poll_now
                try:
                    intake = host_remote_worker.load_remote_intake_config(
                        agentd.STATE_DIR, current_control,
                    )
                    if intake is not None:
                        for task_id in host_remote_worker.pending_host_task_ids(
                            current_control, agentd.STATE_DIR,
                        ):
                            if len(running) >= max_workers:
                                break
                            key = f"@host:{task_id}"
                            if key in running:
                                continue
                            schedule = schedules.setdefault(
                                key, scheduling.RepositorySchedule(),
                            )
                            if host_poll_now < schedule.retry_not_before:
                                continue
                            if start_host_task_worker(
                                current_control, task_id,
                                running=running, max_workers=max_workers,
                            ):
                                schedule.last_poll_at = time.monotonic()
                            else:
                                schedule.retry_not_before = (
                                    time.monotonic() + REPOSITORY_RETRY_SECONDS
                                )
                except (OSError, ValueError, RuntimeError) as exc:
                    log(f"host task intake deferred: {type(exc).__name__}: {exc}")

            if priority_repository is None:
                capacity = max_workers - len(running)
                if capacity > 0:
                    ordered = supervisor_policy.ordered_repositories(
                        repositories,
                        last_repository,
                    )
                    for repository in ordered:
                        if capacity <= 0:
                            break
                        if repository.repository_id in running:
                            continue
                        if repository.repository_id == paused_control_repository:
                            continue
                        if args.once and repository.repository_id in (
                            once_completed | once_failed
                        ):
                            continue
                        schedule = schedules[repository.repository_id]
                        if not args.once and not scheduling.repository_due(schedule, now, polling.settings):
                            continue
                        if args.once and now < schedule.retry_not_before:
                            continue

                        started = start_worker(
                            repository, registry_path=args.registry,
                            running=running, max_workers=max_workers,
                        )
                        schedule.last_poll_at = time.monotonic()
                        if not started:
                            schedule.retry_not_before = (
                                time.monotonic() + REPOSITORY_RETRY_SECONDS
                            )
                            note_once_deferral(repository.repository_id)
                            continue

                        last_repository = repository.repository_id
                        capacity -= 1

            if args.once:
                terminal = once_completed | once_failed
                if enabled.issubset(terminal) and not running and operator_campaign is None:
                    if not host_only and conversation_supervisor.result_publish_pending():
                        if service_control(
                            repositories,
                            registry_path=args.registry,
                            max_workers=max_workers,
                            once=args.once,
                            operator_campaign=operator_campaign,
                        ):
                            last_control_at = time.monotonic()
                            scheduling.reset_control_deferral_state(
                                control_deferrals,
                                clear_pause=True,
                            )
                            continue
                        time.sleep(
                            note_control_deferred(
                                "conversation operator result publication deferred"
                            )
                        )
                        continue
                    return 2 if once_failed else 0

            now = time.monotonic()
            if (
                not running
                and operator_campaign is None
                and not control_pending
                and paused_control_repository is None
                and priority_repository is None
                and scheduling.operator_idle_log_due(last_idle_log_at, now)
            ):
                log(scheduling.format_operator_idle_summary(len(repositories), max_workers))
                last_idle_log_at = now

            if running or operator_campaign is not None or control_pending:
                delay = REAP_INTERVAL_SECONDS
            elif priority_repository is not None:
                retry_at = schedules[priority_repository].retry_not_before
                delay = max(
                    0.05,
                    min(REPOSITORY_RETRY_SECONDS, retry_at - now),
                )
            else:
                repository_delay = scheduling.next_repository_delay(
                    schedules,
                    [
                        repo.repository_id
                        for repo in repositories
                        if repo.repository_id != paused_control_repository
                    ],
                    now, polling.settings,
                )
                control_delay = max(
                    supervisor_policy.interval_remaining(
                        last_control_at,
                        polling.control(now, host_intake_enabled=host_intake_enabled),
                        now,
                    ),
                    max(0.0, control_deferrals.retry_not_before - now),
                )
                delay = min(repository_delay, control_delay, 1.0)
            time.sleep(max(0.05, delay))
        except SystemExit:
            raise
        except Exception as exc:
            log(f"scheduler cycle failed: {type(exc).__name__}: {exc}")
            if args.once:
                return 2
            time.sleep(ERROR_RETRY_SECONDS)


if __name__ == "__main__":
    raise SystemExit(main())
