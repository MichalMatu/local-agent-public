#!/usr/bin/env python3
from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import signal
import threading
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import local_agent.foundation.core as core
from local_agent.operator import local as agent_operator
from local_agent.operator import remote as remote_operator
from local_agent.foundation import storage
import local_agent.daemon.service as agentd
from local_agent.supervisor.macos_release_admission import (
    MacReleaseAdmissionBlocked, macos_task_admission,
)
from local_agent.repository.binding import validate_repository_control_binding
from local_agent.repository.host_admission import HostAdmissionBlocked
from local_agent.repository.host_execution import (
    HostAdmissionUnavailable, check_host_task_admission,
)
from local_agent.foundation.process import (
    ExecutionLeaseBusy,
    LEASE_KEYS_DIGEST_ENV,
    acquire_execution_leases,
    defer_termination,
    execution_lease_path,
    inherited_lease_fds,
    lease_keys_digest,
)
from local_agent.repository.context import (
    RepositoryContext,
    load_repository_registry,
    repository_config_digest,
    repository_lease_keys,
)
from local_agent.version import RELEASE_VERSION
from local_agent.repository.state_layout import recover_claims_with_quarantine_layout
from local_agent.runtime.task_contract import require_task_agent_binding

MULTIREPO_DAEMON_VERSION = RELEASE_VERSION
WORKER_IDLE = 0
WORKER_PROCESSED = 10
WORKER_BUSY = 11
WORKER_CONFIG_CHANGED = 12
CONTROL_WATCH_SECONDS = 1.0
_CONTROL_ID_RE = re.compile(r"^[A-Za-z0-9._-]+$")


def repository_state_dir(repository: RepositoryContext) -> Path:
    root = agentd.STATE_DIR / "repositories" / repository.repository_id
    if repository.host_mailbox_enabled:
        from local_agent.repository.host_routing import canonical_host_id

        return root / "hosts" / canonical_host_id(repository.host_id)
    return root


def repository_lease_dir() -> Path:
    return agentd.STATE_DIR / "locks" / "repositories"


def _validate_inherited_repository_leases(repository: RepositoryContext) -> bool:
    fds = inherited_lease_fds(os.environ)
    if not fds:
        return False
    keys = repository_lease_keys(repository)
    expected_digest = lease_keys_digest(keys)
    if os.environ.get(LEASE_KEYS_DIGEST_ENV) != expected_digest:
        raise RuntimeError("inherited repository lease identity mismatch")
    paths = tuple(execution_lease_path(repository_lease_dir(), key) for key in keys)
    if len(fds) != len(paths):
        raise RuntimeError("inherited repository lease descriptor count mismatch")
    for fd, path in zip(fds, paths, strict=True):
        try:
            descriptor_stat = os.fstat(fd)
            path_stat = path.stat()
        except OSError as exc:
            raise RuntimeError("inherited repository lease is unavailable") from exc
        if (descriptor_stat.st_dev, descriptor_stat.st_ino) != (
            path_stat.st_dev,
            path_stat.st_ino,
        ):
            raise RuntimeError("inherited repository lease file mismatch")
    return True


@contextlib.contextmanager
def repository_execution_lease(repository: RepositoryContext) -> Iterator[None]:
    """Hold every stable identity lock for one complete repository turn."""
    if _validate_inherited_repository_leases(repository):
        yield
        return

    leases = acquire_execution_leases(
        repository_lease_dir(),
        repository_lease_keys(repository),
    )
    updates = leases.environment()
    previous_os = {name: os.environ.get(name) for name in updates}
    previous_core = {name: core.ENV.get(name) for name in updates}
    os.environ.update(updates)
    core.ENV.update(updates)
    try:
        yield
    finally:
        for target, previous in ((os.environ, previous_os), (core.ENV, previous_core)):
            for name, value in previous.items():
                if value is None:
                    target.pop(name, None)
                else:
                    target[name] = value
        leases.close()


def shutdown_handler(signum: int, _frame: Any) -> None:
    if defer_termination(signum):
        return
    core.log(f"received signal {signum}; terminating worker subprocesses")
    agentd.shutdown_runtime_processes()
    raise SystemExit(128 + signum)


def install_signal_handlers() -> None:
    signal.signal(signal.SIGTERM, shutdown_handler)
    signal.signal(signal.SIGINT, shutdown_handler)


def bind_repository(repository: RepositoryContext) -> None:
    """Bind legacy core/daemon globals inside this short-lived worker process only."""
    core.CONTROL = repository.control
    core.WORK = repository.work
    core.CHECKPOINTS = repository.checkpoints
    core.CONTROL_BRANCH = repository.control_branch
    agentd.configure_task_mailbox(
        repository.host_id, enabled=repository.host_mailbox_enabled,
    )
    core.RESULTS_DIR = agentd.REMOTE_RESULTS_DIR

    state_dir = repository_state_dir(repository)
    agentd.CLAIMS_DIR = state_dir / "claims"
    agentd.CORRUPT_CLAIMS_DIR = state_dir / "corrupt-claims"
    agentd.LOCAL_STATUS_PATH = state_dir / "status.json"
    agentd.LOCAL_RUNS_DIR = state_dir / "runs"
    agentd.RESULT_SPOOL_DIR = state_dir / "result-spool"


def validate_repository_checkouts(repository: RepositoryContext) -> None:
    missing: list[str] = []
    if not (repository.control / ".git").exists():
        missing.append(f"control checkout missing: {repository.control}")
    if not (repository.work / ".git").exists():
        missing.append(f"work checkout missing: {repository.work}")
    if missing:
        raise RuntimeError("; ".join(missing))


def repository_status_fields(repository: RepositoryContext) -> dict[str, Any]:
    fields: dict[str, Any] = {
        **repository.status_fields(),
        "execution_model": "multi_repository_worker",
        "worker_pid": os.getpid(),
    }
    supervisor_pid = os.environ.get("LOCAL_AGENT_SUPERVISOR_PID")
    if supervisor_pid:
        try:
            fields["supervisor_pid"] = int(supervisor_pid)
        except ValueError:
            fields["supervisor_pid"] = supervisor_pid
    return fields


_REMOTE_STATUS_MATCH_FIELDS = (
    "state",
    "daemon_version",
    "self_revision",
    "repository_id",
    "repository",
    "agent_binding",
    "control_branch",
    "default_branch",
    "execution_model",
    "supervisor_pid",
    "execution_variant",
)


def _remote_repository_status() -> dict[str, Any]:
    path = core.CONTROL / agentd.REMOTE_DAEMON_STATUS
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError, AttributeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def repository_remote_status_due(
    current: dict[str, Any],
    remote: dict[str, Any],
    *,
    now: datetime | None = None,
) -> bool:
    """Return whether repository status must be refreshed on the remote branch."""
    if not remote:
        return True
    if any(remote.get(field) != current.get(field) for field in _REMOTE_STATUS_MATCH_FIELDS):
        return True

    raw_updated_at = remote.get("updated_at")
    if not isinstance(raw_updated_at, str) or not raw_updated_at:
        return True
    try:
        updated_at = datetime.fromisoformat(raw_updated_at)
    except ValueError:
        return True
    if updated_at.tzinfo is None:
        return True

    reference = now or datetime.now(timezone.utc)
    age = max(
        0.0,
        (reference.astimezone(timezone.utc) - updated_at.astimezone(timezone.utc)).total_seconds(),
    )
    return age >= agentd.REMOTE_HEARTBEAT_SECONDS


def publish_repository_status(
    repository: RepositoryContext,
    state: str,
    *,
    force_remote: bool = True,
    **extra: Any,
) -> None:
    """Persist every local status but throttle idle remote commits across worker processes."""
    remote_status = _remote_repository_status()
    payload = agentd.daemon_status_payload(
        state,
        **repository_status_fields(repository),
        **extra,
    )
    agentd.atomic_write_json(agentd.LOCAL_STATUS_PATH, payload)

    remote_due = force_remote or repository_remote_status_due(payload, remote_status)
    if not remote_due:
        return
    try:
        agentd.publish_control_json(
            agentd.REMOTE_DAEMON_STATUS,
            payload,
            commit_message=f"Agent repository status: {repository.repository_id} {state}",
        )
    except Exception as exc:
        core.log(
            f"remote repository status publish failed repository={repository.repository_id}: "
            f"{type(exc).__name__}: {exc}"
        )


def sync_control_quietly() -> None:
    """Run bounded routine control-branch sync without printing low-level Git commands."""
    storage.sync_control(core, tasks_dir=agentd.REMOTE_TASKS_DIR)


def repository_binding_ready(
    repository: RepositoryContext,
    *,
    execution_variant: str | None = None,
) -> bool:
    """Fail closed unless registry and control branch agree on one binding."""
    status_extra = {"execution_variant": execution_variant} if execution_variant else {}
    if repository.agent_binding is None:
        publish_repository_status(
            repository,
            "unbound",
            force_remote=True,
            error="repository registry is missing agent_binding",
            **status_extra,
        )
        core.log(
            f"repository admission blocked: missing agent_binding "
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
        publish_repository_status(
            repository,
            "binding_error",
            force_remote=True,
            error=str(exc),
            **status_extra,
        )
        core.log(
            f"repository admission blocked by binding check "
            f"repository={repository.repository_id}: {exc}"
        )
        return False
    return True


def reject_task_binding(
    repository: RepositoryContext,
    task: dict[str, object],
) -> None:
    """Publish a terminal pre-claim rejection for a missing or wrong task binding."""
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
        "daemon_version": MULTIREPO_DAEMON_VERSION,
        "repository_id": repository.repository_id,
        "repository": repository.repository,
        "expected_agent_binding": expected,
        "provided_agent_binding": provided,
        "error": "Task rejected before claim because its agent_binding does not match the bound repository.",
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



def reject_host_task(repository: RepositoryContext, task: dict[str, object], reason: str) -> None:
    """Terminally reject a misrouted host task before any claim or effect."""
    task_id = str(task.get("id", ""))
    result = {
        "id": task_id,
        "status": "failed",
        "failure_reason": reason,
        "task_digest": agentd.task_digest(task),
        "started_at": None,
        "finished_at": agentd.now_iso(),
        "daemon_version": MULTIREPO_DAEMON_VERSION,
        "repository_id": repository.repository_id,
        "repository": repository.repository,
        "error": "Host task rejected by the selected mailbox admission policy.",
    }
    core.publish_result(task_id, result)
    agentd.publish_run_state(
        task_id,
        {"event": "task_rejected", "status": "failed", "failure_reason": reason,
         "updated_at": agentd.now_iso()},
        force_remote=True,
    )
    core.log(f"host admission rejected repository={repository.repository_id} "
             f"task={task_id} reason={reason}")


def _control_ack_path(control_id: str) -> Path:
    return core.CONTROL / agentd.REMOTE_CONTROL_ACK_DIR / f"{control_id}.json"


def publish_repository_control_ack(
    repository: RepositoryContext,
    control_id: str,
    action: str,
    status: str,
    **extra: Any,
) -> None:
    payload: dict[str, Any] = {
        "id": control_id,
        "action": action,
        "status": status,
        "daemon_version": MULTIREPO_DAEMON_VERSION,
        "updated_at": agentd.now_iso(),
        **repository.status_fields(),
        **extra,
    }
    agentd.publish_control_json(
        f"{agentd.REMOTE_CONTROL_ACK_DIR}/{control_id}.json",
        payload,
        commit_message=f"Agent repository control ack: {control_id}",
    )


def _read_repository_control_request() -> dict[str, Any] | None:
    path = core.CONTROL / agentd.REMOTE_CONTROL_REQUEST
    if not path.exists():
        return None
    try:
        request = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        core.log(f"invalid repository control request: {type(exc).__name__}: {exc}")
        return None
    if not isinstance(request, dict):
        core.log("invalid repository control request: root must be an object")
        return None
    control_id = str(request.get("id", ""))
    if (
        not control_id
        or len(control_id) > 120
        or not _CONTROL_ID_RE.fullmatch(control_id)
    ):
        return None
    return request


def _control_already_acknowledged(control_id: str) -> bool:
    try:
        return agentd.control_ack_published(control_id)
    except Exception as exc:
        core.log(
            f"repository control ACK verification failed id={control_id}: "
            f"{type(exc).__name__}: {exc}"
        )
        return True


def _cancel_pending_task(
    repository: RepositoryContext,
    control_id: str,
    task_id: str,
) -> bool:
    pending = {str(task.get("id", "")): task for _, task in agentd.pending_tasks()}
    task = pending.get(task_id)
    if task is None:
        results_dir = core.CONTROL / agentd.REMOTE_RESULTS_DIR
        if (results_dir / f"{task_id}.json").exists():
            publish_repository_control_ack(
                repository,
                control_id,
                "cancel_task",
                "completed",
                task_id=task_id,
                result="already_terminal",
            )
            return True
        publish_repository_control_ack(
            repository,
            control_id,
            "cancel_task",
            "rejected",
            task_id=task_id,
            result="task_not_pending",
        )
        return True

    result = {
        "id": task_id,
        "status": "failed",
        "failure_reason": "cancelled_by_operator",
        "task_digest": agentd.task_digest(task),
        "started_at": None,
        "finished_at": agentd.now_iso(),
        "daemon_version": MULTIREPO_DAEMON_VERSION,
        "error": "Task cancelled by operator before execution.",
    }
    core.publish_result(task_id, result)
    agentd.publish_run_state(
        task_id,
        {
            "event": "cancelled_before_execution",
            "status": "failed",
            "failure_reason": "cancelled_by_operator",
            "control_id": control_id,
            "updated_at": agentd.now_iso(),
        },
        force_remote=True,
    )
    publish_repository_control_ack(
        repository,
        control_id,
        "cancel_task",
        "completed",
        task_id=task_id,
        result="cancelled_before_execution",
    )
    return True


def _handle_disable_control(
    repository: RepositoryContext,
    control_id: str,
    *,
    terminate_self: bool,
) -> bool:
    agent_operator.disable_agent(
        control_id=control_id,
        repository_id=repository.repository_id,
        reason="remote_control",
    )
    publish_repository_control_ack(
        repository,
        control_id,
        "disable",
        "completed",
        result="agent_disabled",
    )
    core.log(
        f"operator disabled Local Agent repository={repository.repository_id} "
        f"control={control_id}"
    )
    if terminate_self:
        os.kill(os.getpid(), signal.SIGTERM)
    return True


def handle_repository_control(repository: RepositoryContext) -> None:
    """Handle repository-scoped controls before task admission."""
    request = _read_repository_control_request()
    if request is None:
        return
    control_id = str(request["id"])
    action = str(request.get("action", ""))

    if action in {"restart", "self_update"}:
        # These are global supervisor actions. A fast per-repository worker must
        # leave them unacknowledged so the supervisor can own the request.
        return

    if _control_already_acknowledged(control_id):
        return

    if action == "disable":
        _handle_disable_control(repository, control_id, terminate_self=False)
        return

    if action == "cancel_task":
        task_id = str(request.get("task_id", ""))
        if not task_id or not agentd.valid_control_id(task_id):
            publish_repository_control_ack(
                repository,
                control_id,
                action,
                "rejected",
                result="invalid_task_id",
            )
            return
        _cancel_pending_task(repository, control_id, task_id)
        return

    if action == "status":
        publish_repository_status(
            repository,
            "disabled" if agent_operator.is_disabled() else "idle",
            force_remote=True,
            control_id=control_id,
        )
        publish_repository_control_ack(
            repository,
            control_id,
            action,
            "completed",
            result="status_published",
        )
        return

    publish_repository_control_ack(
        repository,
        control_id,
        action,
        "rejected",
        result="unsupported_action",
    )


class ActiveRepositoryControlWatcher:
    """Watch one active task's own control branch for emergency controls."""

    def __init__(self, repository: RepositoryContext, task_id: str) -> None:
        self.repository = repository
        self.task_id = task_id
        self._stop = threading.Event()
        self._remote_operator = remote_operator.RemoteOperatorState()
        self._thread = threading.Thread(
            target=self._run,
            daemon=True,
            name=f"local-agent-control-{repository.repository_id}",
        )

    def __enter__(self) -> ActiveRepositoryControlWatcher:
        self._thread.start()
        return self

    def __exit__(self, _exc_type: object, _exc: object, _tb: object) -> None:
        self._stop.set()
        self._thread.join(timeout=2.0)

    def _run(self) -> None:
        while not self._stop.wait(CONTROL_WATCH_SECONDS):
            try:
                if self.repository.host_mailbox_enabled:
                    remote_operator.poll_remote_operator(
                        self._remote_operator, self_repo=agentd.SELF_REPO,
                    )
                if agent_operator.is_disabled():
                    core.log("operator disabled active task; terminating without replay")
                    os.kill(os.getpid(), signal.SIGTERM)
                    return
                sync_control_quietly()
                request = _read_repository_control_request()
                if request is None:
                    continue
                control_id = str(request["id"])
                action = str(request.get("action", ""))
                if action not in {"cancel_task", "disable"}:
                    continue
                if _control_already_acknowledged(control_id):
                    continue

                if action == "disable":
                    _handle_disable_control(
                        self.repository,
                        control_id,
                        terminate_self=True,
                    )
                    return

                requested_task_id = str(request.get("task_id", ""))
                if requested_task_id != self.task_id:
                    publish_repository_control_ack(
                        self.repository,
                        control_id,
                        action,
                        "rejected",
                        task_id=requested_task_id,
                        active_task_id=self.task_id,
                        result="active_task_mismatch",
                    )
                    continue

                publish_repository_control_ack(
                    self.repository,
                    control_id,
                    action,
                    "accepted",
                    task_id=self.task_id,
                    result="terminating_active_task",
                )
                core.log(
                    f"operator cancelling active task repository={self.repository.repository_id} "
                    f"task={self.task_id} control={control_id}"
                )
                os.kill(os.getpid(), signal.SIGTERM)
                return
            except BaseException as exc:
                if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                    return
                core.log(
                    f"active repository control watcher degraded "
                    f"repository={self.repository.repository_id}: "
                    f"{type(exc).__name__}: {exc}"
                )


def poll_repository_once(repository: RepositoryContext) -> bool:
    """Admit one entire project turn under the optional macOS shared release lease."""
    with macos_task_admission(agentd.STATE_DIR):
        return _poll_repository_once_admitted(repository)


def _poll_repository_once_admitted(repository: RepositoryContext) -> bool:
    """Poll one repository and execute at most one task."""
    bind_repository(repository)
    validate_repository_checkouts(repository)
    previous_version = agentd.DAEMON_VERSION
    agentd.DAEMON_VERSION = MULTIREPO_DAEMON_VERSION
    try:
        sync_control_quietly()
        if agent_operator.is_disabled():
            publish_repository_status(
                repository,
                "disabled",
                force_remote=True,
            )
            return False
        if not repository_binding_ready(repository):
            return False
        recover_claims_with_quarantine_layout(agentd.recover_stale_claims, agentd.CORRUPT_CLAIMS_DIR, agentd.STATE_DIR)
        agentd.recover_invalid_task_files()
        handle_repository_control(repository)
        if agent_operator.is_disabled():
            publish_repository_status(
                repository, "disabled", force_remote=True,
            )
            return False
        pending = agentd.pending_tasks()
        if not pending:
            state = "publication_pending" if agentd.has_pending_publications() else "idle"
            publish_repository_status(
                repository,
                state,
                force_remote=False,
            )
            return False

        _, task = pending[0]
        task_id = str(task.get("id", ""))
        try:
            assert repository.agent_binding is not None
            require_task_agent_binding(task, repository.agent_binding)
        except ValueError:
            reject_task_binding(repository, task)
            return True
        try:
            check_host_task_admission(
                repository, task, pending[0][0],
                operator_disabled=agent_operator.is_disabled(),
            )
        except HostAdmissionUnavailable:
            # An unavailable/revoked grant is a pause, not a terminal result.
            publish_repository_status(repository, "host_admission_waiting", force_remote=False)
            return False
        except HostAdmissionBlocked as exc:
            reject_host_task(repository, task, exc.reason)
            return True
        core.log(
            f"multi-repo dispatch repository={repository.repository_id} "
            f"task={task_id} binding={repository.agent_binding}"
        )
        from local_agent.foundation.attached_checkout import execute_in_task_workspace

        with ActiveRepositoryControlWatcher(repository, task_id):
            outcome = execute_in_task_workspace(
                repository, core, task, agentd.execute_task,
            )
        state = "publication_pending" if outcome == "publication_pending" else "idle"
        publish_repository_status(
            repository,
            state,
            force_remote=True,
            last_task_id=task_id,
        )
        return True
    finally:
        agentd.DAEMON_VERSION = previous_version


def repository_by_id(
    repository_id: str,
    *,
    registry_path: Path | None,
    expected_config_digest: str,
) -> RepositoryContext:
    repositories = load_repository_registry(path=registry_path)
    for repository in repositories:
        if repository.repository_id == repository_id:
            if repository_config_digest(repository) != expected_config_digest:
                raise ValueError(
                    f"repository configuration changed before worker start: "
                    f"{repository_id!r}"
                )
            return repository
    raise ValueError(f"repository id is not enabled: {repository_id!r}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Poll one configured local-agent repository once.")
    parser.add_argument("--repository-id", required=True)
    parser.add_argument("--registry", type=Path)
    parser.add_argument("--expected-config-digest", required=True)
    return parser.parse_args()


def main() -> int:
    install_signal_handlers()
    args = parse_args()
    try:
        repository = repository_by_id(
            args.repository_id,
            registry_path=args.registry,
            expected_config_digest=args.expected_config_digest,
        )
    except ValueError as exc:
        core.log(str(exc))
        return WORKER_CONFIG_CHANGED
    try:
        with repository_execution_lease(repository):
            processed = poll_repository_once(repository)
    except MacReleaseAdmissionBlocked as exc:
        core.log(f"repository turn deferred by Mac release fence: {exc}")
        return WORKER_BUSY
    except ExecutionLeaseBusy as exc:
        core.log(
            f"repository execution lease busy repository={repository.repository_id} "
            f"key={exc.key}"
        )
        return WORKER_BUSY
    return WORKER_PROCESSED if processed else WORKER_IDLE

if __name__ == "__main__":
    raise SystemExit(main())
