"""Guarded Local Agent service entrypoint with repository-independent emergency stop."""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path

import local_agent.daemon.service as agentd
from local_agent.daemon.installation import installation_pending, installation_transaction
from local_agent.foundation.lease_recovery import (
    recover_orphaned_repository_leases,
    repository_lease_paths,
    repository_leases_busy,
)
from local_agent.foundation.process import (
    INHERITED_STOP_ENV, popen_registered, terminate_process_group, unregister_process,
)
from local_agent.repository.admin import provision_repository
from local_agent.version import RELEASE_VERSION
from local_agent.paths import repository_root
from local_agent.operator import local as agent_operator
from local_agent.operator import remote as agent_remote_operator
from local_agent.repository.context import RepositoryContext, load_repository_registry

REPO_ROOT = repository_root()
LOOP_SECONDS = 0.5
DISABLED_STATUS_SECONDS = 5.0
QUIESCENT_LEASE_STALL_SECONDS = 30.0
_stop_requested = False


def log(message: str) -> None:
    agentd.log(f"[entrypoint] {message}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Guarded Local Agent launcher")
    parser.add_argument("--registry", type=Path)
    parser.add_argument("--max-workers", type=int, default=1)
    return parser.parse_args()


def _signal_handler(signum: int, _frame: object) -> None:
    global _stop_requested
    log(f"received signal {signum}; stopping guarded supervisor")
    _stop_requested = True
    # A caught signal is reset to its default disposition by exec(). Preserve the
    # shutdown request explicitly so a revision re-exec already in flight cannot
    # turn a launchd bootout into a fresh daemon instance.
    os.environ[INHERITED_STOP_ENV] = "1"


def install_signal_handlers() -> None:
    signal.signal(signal.SIGTERM, _signal_handler)
    signal.signal(signal.SIGINT, _signal_handler)


def _consume_inherited_stop_request() -> bool:
    global _stop_requested
    inherited = os.environ.pop(INHERITED_STOP_ENV, None) == "1"
    if inherited:
        _stop_requested = True
    return inherited


def _cleanup_generated_python_noise(repository: RepositoryContext) -> int:
    root = repository.control / ".agent" / "patches"
    if not root.exists():
        return 0
    removed = 0
    for directory in sorted(root.rglob("__pycache__"), reverse=True):
        if directory.is_dir():
            removed += sum(1 for item in directory.rglob("*") if item.is_file())
            shutil.rmtree(directory)
    for suffix in ("*.pyc", "*.pyo"):
        for path in root.rglob(suffix):
            if path.is_file():
                path.unlink()
                removed += 1
    return removed


def prepare_repositories(repositories: list[RepositoryContext]) -> None:
    """Repair only bounded generated noise and explicitly missing checkouts."""
    for repository in repositories:
        removed = _cleanup_generated_python_noise(repository)
        if removed:
            log(
                f"removed generated control bytecode repository={repository.repository_id} "
                f"files={removed}"
            )
        control_missing = not (repository.control / ".git").exists()
        work_missing = not (repository.work / ".git").exists()
        if control_missing or work_missing:
            log(
                f"provisioning missing repository checkout repository={repository.repository_id} "
                f"control_missing={control_missing} work_missing={work_missing}"
            )
            provision_repository(repository)


def supervisor_command(args: argparse.Namespace) -> list[str]:
    command = [
        sys.executable,
        "-m",
        "local_agent.supervisor.orchestrator",
    ]
    if args.registry is not None:
        command.extend(["--registry", str(args.registry)])
    command.extend(["--max-workers", str(args.max_workers)])
    return command


def start_supervisor(args: argparse.Namespace) -> subprocess.Popen[str]:
    env = os.environ.copy()
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    proc = popen_registered(
        supervisor_command(args),
        cwd=REPO_ROOT,
        env=env,
        text=True,
        start_new_session=True,
    )
    log(f"started parallel supervisor pid={proc.pid}")
    return proc


def stop_supervisor(proc: subprocess.Popen[str] | None) -> None:
    if proc is None:
        return
    try:
        terminate_process_group(proc, log)
    finally:
        unregister_process(proc)


def publish_guard_status(state: str, *, max_workers: int) -> None:
    payload = agentd.daemon_status_payload(
        state,
        execution_model="guarded_parallel_entrypoint",
        entrypoint_pid=os.getpid(),
        supervisor_pid=None,
        max_parallel_workers=max_workers,
        active_repository_ids=[],
    )
    agentd.atomic_write_json(agentd.LOCAL_STATUS_PATH, payload)


def _self_reexec_args(args: argparse.Namespace) -> list[str]:
    command = [sys.executable, str(REPO_ROOT / "agent_entrypoint.py")]
    if args.registry is not None:
        command.extend(["--registry", str(args.registry)])
    command.extend(["--max-workers", str(args.max_workers)])
    return command


def _quiescent_supervisor_status(supervisor_pid: int) -> dict[str, object] | None:
    """Return a snapshot only for the scheduler's current idle status.

    Successful global-control work publishes `supervisor_control_repository`, so
    that state must never be mistaken for an orphaned repository lease.
    """
    try:
        payload = json.loads(agentd.LOCAL_STATUS_PATH.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None
    if not isinstance(payload, dict):
        return None
    if (
        payload.get("state") == "idle"
        and payload.get("supervisor_pid") == supervisor_pid
        and payload.get("active_repository_ids") == []
        and "supervisor_control_repository" not in payload
    ):
        return payload
    return None


def _quiescent_lease_observation(supervisor_pid: int, paths: tuple[Path, ...]) -> bool | None:
    """Accept a lock probe only while the same idle snapshot remains current."""
    before = _quiescent_supervisor_status(supervisor_pid)
    if before is None:
        return None
    busy = repository_leases_busy(paths)
    if _quiescent_supervisor_status(supervisor_pid) != before:
        return None
    return busy


def _updated_quiescent_lease_watch(
    started_at: float | None,
    *,
    supervisor_quiescent: bool,
    leases_busy: bool,
    now: float,
) -> float | None:
    """Advance the stale-lease watch only from verified quiescent observations.

    Active workers and global-control polls are transient scheduler activity. They
    must not erase an already observed stale lease because unrelated repository
    turns can otherwise reset the 30-second watch forever. Destructive recovery
    still requires a later verified quiescent+busy observation.
    """
    if not supervisor_quiescent:
        return started_at
    if not leases_busy:
        return None
    return now if started_at is None else started_at


@contextlib.contextmanager
def _orphan_recovery_guard() -> Iterator[None]:
    """Exclude every other Local Agent daemon while orphan cleanup is destructive."""
    agentd.DAEMON_LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    handle = agentd.DAEMON_LOCK_PATH.open("a+", encoding="utf-8")
    locked = False
    try:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            locked = True
        except BlockingIOError as exc:
            raise RuntimeError(
                "another local-agent daemon holds the daemon lock; refusing orphan recovery"
            ) from exc
        yield
    finally:
        if locked:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


def _recover_orphaned_leases_safely(paths: tuple[Path, ...]) -> tuple[int, ...]:
    """Recover stale repository holders only while owning the global daemon lock."""
    with _orphan_recovery_guard():
        return recover_orphaned_repository_leases(paths, log=log)


def _recover_before_supervisor_start(repositories: list[RepositoryContext]) -> None:
    """Clear only orphaned holders while no supervisor or worker is running."""
    paths = repository_lease_paths(repositories, state_dir=agentd.STATE_DIR)
    if not repository_leases_busy(paths):
        return
    recovered = _recover_orphaned_leases_safely(paths)
    if recovered:
        log(f"recovered orphaned repository leases before supervisor start pids={list(recovered)}")


def recover_interrupted_installation() -> tuple[bool, str]:
    """Recover a valid interrupted self-update without clearing operator disables."""
    recovered, detail = agentd.recover_interrupted_self_update()
    if not recovered:
        return False, detail
    cleared = agent_operator.clear_interrupted_self_update_disable()
    log(
        "interrupted self-update recovery completed "
        f"detail={detail} automatic_disable_cleared={cleared}"
    )
    return True, detail


def main() -> int:
    global _stop_requested

    args = parse_args()
    install_signal_handlers()
    if _consume_inherited_stop_request():
        log("inherited shutdown request after self re-exec; exiting without supervisor start")
        return 0

    initial_revision = agentd.self_revision()
    remote = agent_remote_operator.RemoteOperatorState()
    child: subprocess.Popen[str] | None = None
    managed_repositories: list[RepositoryContext] = []
    quiescent_lease_busy_since: float | None = None
    last_disabled_status = 0.0

    log(
        f"guarded entrypoint {RELEASE_VERSION} starting max_workers={args.max_workers} "
        f"revision={initial_revision}"
    )

    try:
        while not _stop_requested:
            try:
                agent_remote_operator.poll_remote_operator(
                    remote,
                    self_repo=agentd.SELF_REPO,
                )
            except Exception as exc:
                log(f"remote operator poll degraded: {type(exc).__name__}: {exc}")

            current_revision = agentd.self_revision()
            pending_installation = installation_pending(agentd.STATE_DIR)
            if pending_installation:
                recovered, detail = recover_interrupted_installation()
                if recovered:
                    pending_installation = False
                    current_revision = agentd.self_revision()
                else:
                    if detail == "installation_lock_busy":
                        # The supervisor may be installing right now. Do not kill
                        # an active installer merely because its journal exists.
                        # An explicit emergency disable still takes precedence.
                        if agent_operator.is_disabled():
                            stop_supervisor(child)
                            child = None
                            quiescent_lease_busy_since = None
                        time.sleep(LOOP_SECONDS)
                        continue
                    if not agent_operator.is_disabled():
                        agent_operator.disable_agent(reason="interrupted_self_update")
                    log(f"interrupted self-update recovery deferred: {detail}")
                    stop_supervisor(child)
                    child = None
                    quiescent_lease_busy_since = None
                    time.sleep(LOOP_SECONDS)
                    continue

            if (
                initial_revision is not None
                and current_revision is not None
                and current_revision != initial_revision
            ):
                with installation_transaction(agentd.STATE_DIR) as acquired:
                    if acquired:
                        # The updater may have rolled back while the guard was
                        # waiting. Re-read only after validation releases its lock.
                        current_revision = agentd.self_revision()
                        if (
                            not installation_pending(agentd.STATE_DIR)
                            and current_revision is not None
                            and current_revision != initial_revision
                        ):
                            if _stop_requested:
                                log("shutdown requested; suppressing guarded self re-exec")
                                break
                            log(
                                f"self revision changed {initial_revision} -> {current_revision}; "
                                "re-executing guarded entrypoint"
                            )
                            stop_supervisor(child)
                            child = None
                            # If SIGTERM/SIGINT lands after the check above but before
                            # exec(), the signal handler records INHERITED_STOP_ENV.
                            # exec preserves that environment marker and the new image
                            # exits before starting another supervisor.
                            os.execv(sys.executable, _self_reexec_args(args))

            if _stop_requested:
                break

            if agent_operator.is_disabled():
                if child is not None:
                    stop_supervisor(child)
                    child = None
                    quiescent_lease_busy_since = None
                now = time.monotonic()
                if now - last_disabled_status >= DISABLED_STATUS_SECONDS:
                    publish_guard_status("disabled", max_workers=args.max_workers)
                    last_disabled_status = now
                time.sleep(LOOP_SECONDS)
                continue

            if child is not None and child.poll() is not None:
                log(f"parallel supervisor exited code={child.returncode}; scheduling restart")
                stop_supervisor(child)
                child = None
                quiescent_lease_busy_since = None

            if child is not None and managed_repositories:
                try:
                    paths = repository_lease_paths(
                        managed_repositories,
                        state_dir=agentd.STATE_DIR,
                    )
                    leases_busy = _quiescent_lease_observation(child.pid, paths)
                    if leases_busy is not None:
                        now = time.monotonic()
                        previous_watch = quiescent_lease_busy_since
                        quiescent_lease_busy_since = _updated_quiescent_lease_watch(
                            previous_watch,
                            supervisor_quiescent=True,
                            leases_busy=leases_busy,
                            now=now,
                        )
                        if leases_busy:
                            if previous_watch is None:
                                log(
                                    "quiescent supervisor has busy repository lease; "
                                    "starting bounded recovery watch"
                                )
                            elif (
                                quiescent_lease_busy_since is not None
                                and now - quiescent_lease_busy_since
                                >= QUIESCENT_LEASE_STALL_SECONDS
                            ):
                                log(
                                    "quiescent repository lease remained busy; "
                                    "recycling supervisor for orphan recovery"
                                )
                                stop_supervisor(child)
                                child = None
                                quiescent_lease_busy_since = None
                                _recover_orphaned_leases_safely(paths)
                                continue
                    else:
                        quiescent_lease_busy_since = _updated_quiescent_lease_watch(
                            quiescent_lease_busy_since,
                            supervisor_quiescent=False,
                            leases_busy=False,
                            now=0.0,
                        )
                except Exception as exc:
                    log(f"lease recovery watchdog degraded: {type(exc).__name__}: {exc}")
                    if child is None:
                        time.sleep(2.0)
                        continue

            if child is None:
                if pending_installation:
                    time.sleep(LOOP_SECONDS)
                    continue
                try:
                    from local_agent.supervisor.host_control import (
                        load_host_control_source, select_global_control,
                    )

                    host_enabled = load_host_control_source(
                        agentd.STATE_DIR / "supervisor-control.json",
                    ) is not None
                    if host_enabled:
                        repositories = load_repository_registry(
                            path=args.registry, allow_empty=True,
                        )
                    else:
                        repositories = load_repository_registry(path=args.registry)
                    # The machine control stream is independent of projects,
                    # but must be configured, trusted and provisioned first.
                    select_global_control(
                        repositories, state_dir=agentd.STATE_DIR,
                        production_checkout=agentd.SELF_REPO,
                    )
                    prepare_repositories(repositories)
                    _recover_before_supervisor_start(repositories)
                    child = start_supervisor(args)
                    managed_repositories = repositories
                    quiescent_lease_busy_since = None
                except Exception as exc:
                    log(f"guarded supervisor start deferred: {type(exc).__name__}: {exc}")
                    time.sleep(2.0)
                    continue

            time.sleep(LOOP_SECONDS)

    finally:
        if child is not None:
            stop_supervisor(child)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
