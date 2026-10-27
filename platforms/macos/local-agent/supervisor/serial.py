#!/usr/bin/env python3
from __future__ import annotations

import argparse
import contextlib
import os
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from local_agent.paths import repository_root

import local_agent.daemon.service as agentd
from local_agent.operator import local as agent_operator
from local_agent.operator import remote as remote_operator
from local_agent.foundation.process import (
    ExecutionLeaseBusy,
    INHERITED_STOP_ENV,
    LEASE_FDS_ENV,
    LEASE_KEYS_DIGEST_ENV,
    RESOURCE_LEASE_FDS_ENV,
    defer_termination,
    popen_registered,
    terminate_active_processes,
    terminate_process_group,
    unregister_process,
)
from local_agent.repository.context import (
    RepositoryContext,
    load_repository_registry,
    repository_config_digest,
)
from local_agent.repository.worker import (
    WORKER_BUSY,
    WORKER_CONFIG_CHANGED,
    WORKER_IDLE,
    WORKER_PROCESSED,
    repository_execution_lease,
)
from local_agent.supervisor.worker import WORKER_MACHINE_BUSY, WORKER_RESOURCE_BUSY
from local_agent.supervisor import host_control as host_supervisor_control
from local_agent.supervisor import host_remote_worker
from local_agent.version import RELEASE_VERSION
from local_agent.supervisor.control import (
    bind_supervisor_control as bind_shared_supervisor_control,
    sync_control_quietly,
)
from local_agent.supervisor.policy import (
    HOT_POLL_SECONDS,
    HOT_WINDOW_SECONDS,
    POLL_SECONDS,
    SUPERVISOR_CONTROL_POLL_SECONDS,
    WARM_POLL_SECONDS,
    WARM_WINDOW_SECONDS,
    WORKER_TURN_GRACE_SECONDS,
    adaptive_poll_tier,
    interval_due,
    interval_remaining,
    ordered_repositories,
)

SUPERVISOR_VERSION = RELEASE_VERSION
_daemon_lock_handle: Any | None = None
_stop_requested = threading.Event()
_stop_deadline: float | None = None


def log(message: str) -> None:
    agentd.log(f"[multi-repo] {message}")


def format_idle_summary(repository_count: int) -> str:
    noun = "repository" if repository_count == 1 else "repositories"
    return f"no pending task ({repository_count} {noun})"


def scheduler_sleep_seconds(
    *,
    active_repository: str | None,
    last_activity_at: float | None,
    last_active_poll_at: float | None,
    last_full_scan_at: float | None,
    last_control_service_at: float | None,
    now: float,
) -> float:
    """Wake for the earliest repository, full-scan or supervisor-control deadline."""
    if active_repository is not None:
        _, interval = adaptive_poll_tier(last_activity_at, now)
        return min(
            interval_remaining(last_active_poll_at, interval, now),
            interval_remaining(last_full_scan_at, POLL_SECONDS, now),
            interval_remaining(
                last_control_service_at,
                SUPERVISOR_CONTROL_POLL_SECONDS,
                now,
            ),
        )
    return min(
        interval_remaining(last_full_scan_at, POLL_SECONDS, now),
        interval_remaining(
            last_control_service_at,
            SUPERVISOR_CONTROL_POLL_SECONDS,
            now,
        ),
    )


def scheduler_due_actions(
    *,
    active_repository: str | None,
    last_activity_at: float | None,
    last_active_poll_at: float | None,
    last_full_scan_at: float | None,
    last_control_service_at: float | None,
    now: float,
) -> tuple[str, ...]:
    """Select due work with control and full-scan priority over hot polling."""
    actions: list[str] = []
    if interval_due(
        last_control_service_at,
        SUPERVISOR_CONTROL_POLL_SECONDS,
        now,
    ):
        actions.append("control")
    full_scan_due = interval_due(last_full_scan_at, POLL_SECONDS, now)
    if full_scan_due:
        actions.append("full_scan")
    elif active_repository is not None:
        _, active_interval = adaptive_poll_tier(last_activity_at, now)
        if interval_due(last_active_poll_at, active_interval, now):
            actions.append("active_poll")
    return tuple(actions)


def load_registered_projects(*, registry_path: Path | None) -> list[RepositoryContext]:
    """Preserve legacy worker selection; enable empty lists only for host mode."""
    host_enabled = host_supervisor_control.load_host_control_source(
        agentd.STATE_DIR / "supervisor-control.json",
    ) is not None
    if host_enabled:
        return load_repository_registry(path=registry_path, allow_empty=True)
    return load_repository_registry(path=registry_path)


def supervisor_control_repository(
    *,
    registry_path: Path | None,
) -> RepositoryContext | host_supervisor_control.HostControlSource:
    repositories = load_registered_projects(registry_path=registry_path)
    return host_supervisor_control.select_global_control(
        repositories, state_dir=agentd.STATE_DIR, production_checkout=agentd.SELF_REPO,
    )


def bind_supervisor_control(repository: RepositoryContext) -> None:
    bind_shared_supervisor_control(
        repository,
        daemon_version=SUPERVISOR_VERSION,
    )


def supervisor_status_fields(repository: RepositoryContext) -> dict[str, Any]:
    return {
        "execution_model": "multi_repository_supervisor",
        "supervisor_pid": os.getpid(),
        "supervisor_control_repository": repository.repository_id,
    }


def supervisor_restart_command(
    *,
    registry_path: Path | None,
    once: bool,
) -> list[str]:
    """Return the exact supervisor command that must survive restart/self-update."""
    command = [sys.executable, str(repository_root() / "agent_multirepo.py")]
    if registry_path is not None:
        command.extend(["--registry", str(registry_path)])
    if once:
        command.append("--once")
    return command


def restart_supervisor(
    reason: str,
    *,
    registry_path: Path | None,
    once: bool,
) -> None:
    """Restart the multi-repository entrypoint without falling through agentd.py."""
    if _stop_requested.is_set():
        raise SystemExit(0)
    agentd.publish_daemon_status("restarting", force_remote=True, reason=reason)
    log(f"restarting supervisor: {reason}")
    for name in (LEASE_FDS_ENV, LEASE_KEYS_DIGEST_ENV, RESOURCE_LEASE_FDS_ENV):
        os.environ.pop(name, None)
        agentd.core.ENV.pop(name, None)
    command = supervisor_restart_command(registry_path=registry_path, once=once)
    try:
        os.execv(command[0], command)
    except OSError as exc:
        log(f"exec restart failed; asking launchd to restart: {exc}")
        raise SystemExit(75) from exc


@contextlib.contextmanager
def route_supervisor_restarts(
    *,
    registry_path: Path | None,
    once: bool,
) -> Iterator[None]:
    """Route shared agentd restart hooks back to this supervisor entrypoint."""
    original_restart = agentd.restart_self

    def routed_restart(reason: str) -> None:
        restart_supervisor(reason, registry_path=registry_path, once=once)

    agentd.restart_self = routed_restart
    try:
        yield
    finally:
        agentd.restart_self = original_restart


def service_supervisor_control(
    repository: RepositoryContext,
    *,
    registry_path: Path | None = None,
    once: bool = False,
    sync: bool = True,
) -> None:
    bind_supervisor_control(repository)
    if sync:
        sync_control_quietly()
    with route_supervisor_restarts(registry_path=registry_path, once=once):
        if isinstance(repository, host_supervisor_control.HostControlSource):
            path = repository.checkout / repository.request_path
            if path.is_file():
                import json

                try:
                    request = host_supervisor_control.require_host_control_request(
                        repository, json.loads(path.read_text(encoding="utf-8")),
                    )
                except (ValueError, OSError) as exc:
                    log(f"host supervisor request rejected: {exc}")
                else:
                    if request["action"] == "disable":
                        control_id = request["id"]
                        if not agentd.control_ack_published(control_id):
                            agent_operator.disable_agent(
                                reason="remote_control", control_id=control_id,
                                repository_id=repository.repository_id,
                            )
                            agentd.publish_control_ack(
                                control_id, "disable", "completed", result="agent_disabled",
                            )
        else:
            agentd.handle_control_request(status_extra=supervisor_status_fields(repository))
        # Host-addressed installations must not self-switch to legacy main.
        if (not isinstance(repository, host_supervisor_control.HostControlSource)
                and not repository.host_mailbox_enabled):
            agentd.maybe_self_update()


def service_supervisor_control_safely(
    repository: RepositoryContext,
    *,
    registry_path: Path | None = None,
    once: bool = False,
    sync: bool = True,
) -> bool:
    try:
        lease = (
            host_supervisor_control.host_supervisor_lease(repository)
            if isinstance(repository, host_supervisor_control.HostControlSource)
            else repository_execution_lease(repository)
        )
        with lease:
            service_supervisor_control(
                repository,
                registry_path=registry_path,
                once=once,
                sync=sync,
            )
    except ExecutionLeaseBusy:
        log(
            f"supervisor control deferred repository="
            f"{repository.repository_id}: execution lease busy"
        )
        return False
    except Exception as exc:
        log(
            f"supervisor control service degraded repository="
            f"{repository.repository_id}: {type(exc).__name__}: {exc}"
        )
        return False
    return True


def worker_command(
    repository: RepositoryContext,
    *,
    registry_path: Path | None,
) -> list[str]:
    """Use the shared hardened worker even when supervisor concurrency is one."""
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


def run_worker(
    repository: RepositoryContext,
    *,
    registry_path: Path | None,
) -> int:
    if _stop_requested.is_set():
        return WORKER_IDLE
    try:
        with repository_execution_lease(repository):
            if _stop_requested.is_set():
                return WORKER_IDLE
            command = worker_command(repository, registry_path=registry_path)
            env = os.environ.copy()
            env["LOCAL_AGENT_SUPERVISOR_PID"] = str(os.getpid())
            proc = popen_registered(
                command,
                cwd=repository_root(),
                env=env,
                text=True,
                start_new_session=True,
            )
            setattr(proc, "_local_agent_process_group", proc.pid)
            try:
                turn_limit = agentd.TIMEOUTS.task_max + WORKER_TURN_GRACE_SECONDS
                turn_deadline = time.monotonic() + turn_limit
                while True:
                    deadline = turn_deadline
                    if _stop_deadline is not None:
                        deadline = min(deadline, _stop_deadline)
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        reason = "stop grace expired" if _stop_requested.is_set() else "timed out"
                        log(f"repository worker {reason} repository={repository.repository_id}")
                        terminate_process_group(proc, log)
                        return 128 + signal.SIGTERM if _stop_requested.is_set() else 124
                    try:
                        return proc.wait(timeout=min(0.5, remaining))
                    except subprocess.TimeoutExpired:
                        continue
            finally:
                unregister_process(proc)
    except ExecutionLeaseBusy:
        return WORKER_BUSY


def run_host_task_turn(task_id: str) -> int:
    """Run one opted-in machine task without an invented project lease."""
    if _stop_requested.is_set() or agent_operator.is_disabled():
        return WORKER_IDLE
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
    try:
        deadline = time.monotonic() + agentd.TIMEOUTS.task_max + WORKER_TURN_GRACE_SECONDS
        while True:
            target = min(deadline, _stop_deadline) if _stop_deadline is not None else deadline
            remaining = target - time.monotonic()
            if remaining <= 0:
                terminate_process_group(proc, log)
                return 128 + signal.SIGTERM if _stop_requested.is_set() else 124
            try:
                return proc.wait(timeout=min(0.5, remaining))
            except subprocess.TimeoutExpired:
                continue
    finally:
        unregister_process(proc)


def shutdown_handler(signum: int, _frame: Any) -> None:
    global _stop_deadline
    if signum == signal.SIGTERM:
        # Draining records intent only; a terminating handler requires deferral.
        # An in-flight registered spawn must return its process handle normally.
        if not _stop_requested.is_set():
            os.environ[INHERITED_STOP_ENV] = "1"
            _stop_deadline = time.monotonic() + agentd.TIMEOUTS.stop_grace
            _stop_requested.set()
            log(
                "stop requested; admission closed; waiting for the current worker "
                f"grace={agentd.TIMEOUTS.stop_grace}s"
            )
        return
    if defer_termination(signum):
        return
    log(f"received signal {signum}; terminating active processes")
    terminate_active_processes(log)
    raise SystemExit(128 + signum)


def install_signal_handlers() -> None:
    signal.signal(signal.SIGTERM, shutdown_handler)
    signal.signal(signal.SIGINT, shutdown_handler)


def _worker_deferred(return_code: int) -> bool:
    return return_code in {WORKER_BUSY, WORKER_RESOURCE_BUSY, WORKER_MACHINE_BUSY}


def run_repository_cycle(
    repository_id: str,
    *,
    registry_path: Path | None,
) -> bool:
    """Poll exactly one active repository without waking idle repositories."""
    repositories = load_registered_projects(registry_path=registry_path)
    matches = [item for item in repositories if item.repository_id == repository_id]
    if not matches:
        raise ValueError(f"repository id is not enabled: {repository_id!r}")
    repository = matches[0]
    return_code = run_worker(repository, registry_path=registry_path)
    if return_code == WORKER_PROCESSED:
        log(f"completed repository turn={repository.repository_id}")
        return True
    if return_code == WORKER_IDLE:
        return False
    if _worker_deferred(return_code):
        log(
            f"repository turn deferred repository={repository.repository_id} "
            f"worker_exit={return_code}"
        )
        return False
    if return_code == WORKER_CONFIG_CHANGED:
        log(
            f"repository turn deferred repository={repository.repository_id}: "
            "configuration changed"
        )
        return False
    log(
        f"repository worker failed repository={repository.repository_id} "
        f"exit={return_code}; keeping scheduler alive"
    )
    return False


def run_cycle(
    *,
    registry_path: Path | None,
    start_after: str | None,
) -> tuple[bool, str | None]:
    repositories = load_registered_projects(registry_path=registry_path)
    for repository in ordered_repositories(repositories, start_after):
        if _stop_requested.is_set():
            break
        return_code = run_worker(repository, registry_path=registry_path)
        if return_code == WORKER_PROCESSED:
            log(f"completed repository turn={repository.repository_id}")
            return True, repository.repository_id
        if return_code == WORKER_IDLE:
            continue
        if _worker_deferred(return_code):
            log(
                f"repository turn deferred repository={repository.repository_id} "
                f"worker_exit={return_code}"
            )
            continue
        if return_code == WORKER_CONFIG_CHANGED:
            log(
                f"repository turn deferred repository={repository.repository_id}: "
                "configuration changed"
            )
            continue
        log(
            f"repository worker failed repository={repository.repository_id} "
            f"exit={return_code}; continuing with other repositories"
        )
    return False, start_after


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Single-daemon multi-repository local-agent supervisor."
    )
    parser.add_argument("--registry", type=Path)
    parser.add_argument("--once", action="store_true")
    return parser.parse_args()


def main() -> int:
    global _daemon_lock_handle, _stop_deadline
    if os.environ.pop(INHERITED_STOP_ENV, None) == "1":
        log("inherited shutdown request after re-exec; exiting without worker admission")
        return 0
    _stop_requested.clear()
    _stop_deadline = None
    args = parse_args()
    _daemon_lock_handle = agentd.acquire_daemon_lock()
    install_signal_handlers()
    registry_path = args.registry
    remote_state = remote_operator.RemoteOperatorState()
    last_repository: str | None = None
    active_repository: str | None = None
    last_activity_at: float | None = None
    last_active_poll_at: float | None = None
    last_full_scan_at: float | None = None
    last_control_service_at: float | None = None
    idle_announced = False
    control_repository = supervisor_control_repository(registry_path=registry_path)
    bind_supervisor_control(control_repository)
    log(
        f"supervisor {SUPERVISOR_VERSION} starting; "
        "global execution concurrency=1 "
        f"control_repository={control_repository.repository_id}; "
        f"adaptive_polling=hot:{HOT_POLL_SECONDS:g}s/{HOT_WINDOW_SECONDS:g}s "
        f"warm:{WARM_POLL_SECONDS:g}s/{WARM_WINDOW_SECONDS:g}s "
        f"idle:{POLL_SECONDS:g}s"
    )
    try:
        lease = (
            host_supervisor_control.host_supervisor_lease(control_repository)
            if isinstance(control_repository, host_supervisor_control.HostControlSource)
            else repository_execution_lease(control_repository)
        )
        with lease:
            sync_control_quietly()
            agentd.publish_daemon_status(
                "idle",
                force_remote=True,
                **supervisor_status_fields(control_repository),
            )
    except ExecutionLeaseBusy:
        log(
            f"initial supervisor control service deferred repository="
            f"{control_repository.repository_id}: execution lease busy"
        )
    except Exception as exc:
        log(
            f"initial supervisor control service degraded: "
            f"{type(exc).__name__}: {exc}"
        )
    last_control_service_at = time.monotonic()

    while not _stop_requested.is_set():
        try:
            remote_operator.poll_remote_operator(remote_state, self_repo=agentd.SELF_REPO)
            if _stop_requested.is_set():
                break
            now = time.monotonic()
            if (
                active_repository is not None
                and last_activity_at is not None
                and now - last_activity_at >= WARM_WINDOW_SECONDS
            ):
                log(f"adaptive polling repository={active_repository} tier=idle")
                active_repository = None
                last_activity_at = None
                last_active_poll_at = None

            now = time.monotonic()
            due_actions = scheduler_due_actions(
                active_repository=active_repository,
                last_activity_at=last_activity_at,
                last_active_poll_at=last_active_poll_at,
                last_full_scan_at=last_full_scan_at,
                last_control_service_at=last_control_service_at,
                now=now,
            )
            if "control" in due_actions:
                control_repository = supervisor_control_repository(
                    registry_path=registry_path
                )
                service_supervisor_control_safely(
                    control_repository,
                    registry_path=registry_path,
                    once=args.once,
                )
                last_control_service_at = time.monotonic()

            if _stop_requested.is_set():
                break
            if "full_scan" in due_actions:
                processed, scanned_repository = run_cycle(
                    registry_path=registry_path,
                    start_after=last_repository,
                )
                completed_at = time.monotonic()
                last_full_scan_at = completed_at
                if processed and scanned_repository is not None:
                    last_repository = scanned_repository
                    active_repository = scanned_repository
                    last_activity_at = completed_at
                    last_active_poll_at = completed_at
                    idle_announced = False
                    log(
                        f"adaptive polling repository={active_repository} "
                        f"tier=hot interval={HOT_POLL_SECONDS:g}s"
                    )
                elif not processed:
                    active_repository = None
                    last_activity_at = None
                    last_active_poll_at = None

                if (isinstance(control_repository, host_supervisor_control.HostControlSource)
                        and not _stop_requested.is_set()):
                    try:
                        config = host_remote_worker.load_remote_intake_config(
                            agentd.STATE_DIR, control_repository,
                        )
                        if config is not None:
                            candidates = host_remote_worker.pending_host_task_ids(
                                control_repository, agentd.STATE_DIR, limit=1,
                            )
                            if candidates:
                                task_id = candidates[0]
                                return_code = run_host_task_turn(task_id)
                                log(
                                    f"host task turn={task_id} worker_exit={return_code}"
                                )
                                idle_announced = False
                    except (ValueError, OSError, RuntimeError) as exc:
                        log(
                            f"host task admission deferred: "
                            f"{type(exc).__name__}: {exc}"
                        )
            elif "active_poll" in due_actions:
                assert active_repository is not None
                processed = run_repository_cycle(
                    active_repository,
                    registry_path=registry_path,
                )
                completed_at = time.monotonic()
                last_active_poll_at = completed_at
                if processed:
                    last_repository = active_repository
                    last_activity_at = completed_at
                    idle_announced = False
                    log(
                        f"adaptive polling repository={active_repository} "
                        f"tier=hot interval={HOT_POLL_SECONDS:g}s"
                    )

        except Exception as exc:
            idle_announced = False
            log(f"registry/scheduler cycle failed: {type(exc).__name__}: {exc}")
            if args.once:
                return 2
            _stop_requested.wait(POLL_SECONDS)
            continue

        if args.once:
            return 0
        if _stop_requested.is_set():
            break
        if active_repository is None and not idle_announced:
            repository_count = len(load_registered_projects(registry_path=registry_path))
            log(format_idle_summary(repository_count))
            idle_announced = True

        now = time.monotonic()
        delay = scheduler_sleep_seconds(
            active_repository=active_repository,
            last_activity_at=last_activity_at,
            last_active_poll_at=last_active_poll_at,
            last_full_scan_at=last_full_scan_at,
            last_control_service_at=last_control_service_at,
            now=now,
        )
        _stop_requested.wait(max(0.05, delay))

    log("supervisor stopped; no further work admitted")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
