#!/usr/bin/env python3
from __future__ import annotations

from collections.abc import Callable

import fcntl
import hashlib
import json
import os
import re
import secrets
import signal
import subprocess
import sys
import tempfile
import threading
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import local_agent.foundation.core as core
import local_agent.foundation.storage as storage
from local_agent.config import TIMEOUTS
from local_agent.daemon.installation import (
    begin_installation,
    finish_installation,
    installation_pending,
    installation_transaction,
    read_pending_installation,
    require_completed_installation,
)
from local_agent.foundation.process import (
    LEASE_FDS_ENV,
    LEASE_KEYS_DIGEST_ENV,
    RESOURCE_LEASE_FDS_ENV,
    atomic_write_text,
    defer_termination,
    fsync_directory,
    termination_critical_section,
    terminate_active_processes,
)
from local_agent.runtime.task_contract import (
    DEFAULT_MEMORY_LIMIT_MB,
    MAX_MEMORY_LIMIT_MB,
    MAX_TASK_FILE_BYTES,
    MIN_MEMORY_LIMIT_MB,
    InvalidDedupeMetadata,
    task_digest,
    validate_task,
)
from local_agent.runtime.executor import RuntimeExecutor
from local_agent.runtime.host_claim_snapshot import host_status_fields
from local_agent.version import RELEASE_VERSION
from local_agent.paths import repository_root
from local_agent.supervisor.macos_release_admission import macos_task_admission

DAEMON_VERSION = RELEASE_VERSION
HOME = Path.home()
SELF_REPO = repository_root()
SELF_BRANCH = "main"
SELF_UPDATE_INTERVAL = 60
SELF_UPDATE_VALIDATION_TIMEOUT_SECONDS = 600
POLL_SECONDS = 15
REMOTE_HEARTBEAT_SECONDS = 300
RUN_PROGRESS_SECONDS = 60
RUN_HEARTBEAT_SECONDS = 60
REMOTE_PROGRESS_FLUSH_SECONDS = 65.0
REMOTE_PROGRESS_COALESCE_SECONDS = 0.25
REMOTE_PROGRESS_SHUTDOWN_SECONDS = 4.0

STATE_DIR = HOME / "Library" / "Application Support" / "local-agent"
CLAIMS_DIR = STATE_DIR / "claims"
CORRUPT_CLAIMS_DIR = STATE_DIR / "corrupt-claims"
DAEMON_LOCK_PATH = STATE_DIR / "agentd.lock"
REJECTED_UPDATE_PATH = STATE_DIR / "rejected-self-update.json"
LOCAL_STATUS_PATH = STATE_DIR / "status.json"
LOCAL_RUNS_DIR = STATE_DIR / "runs"
RESULT_SPOOL_DIR = STATE_DIR / "result-spool"

REMOTE_DAEMON_STATUS = ".agent/status/daemon.json"
REMOTE_CONTROL_REQUEST = ".agent/daemon/control.json"
REMOTE_CONTROL_ACK_DIR = ".agent/daemon/acks"
REMOTE_RUNS_DIR = ".agent/runs"
REMOTE_TASKS_DIR = ".agent/tasks"
REMOTE_RESULTS_DIR = ".agent/results"


def configure_task_mailbox(host_id: str | None = None, *, enabled: bool = False) -> None:
    """Select one task/result namespace. Never select a host mailbox implicitly."""
    global REMOTE_TASKS_DIR, REMOTE_RESULTS_DIR, REMOTE_RUNS_DIR
    global REMOTE_DAEMON_STATUS, REMOTE_CONTROL_REQUEST, REMOTE_CONTROL_ACK_DIR
    if type(enabled) is not bool:
        raise ValueError("host mailbox feature gate must be boolean")
    if enabled:
        from local_agent.repository.host_routing import canonical_host_id

        if host_id is None:
            raise ValueError("host mailbox selection requires host_id")
        namespace = f".agent/hosts/{canonical_host_id(host_id)}"
        REMOTE_TASKS_DIR = f"{namespace}/tasks"
        REMOTE_RESULTS_DIR = f"{namespace}/results"
        REMOTE_RUNS_DIR = f"{namespace}/runs"
        REMOTE_DAEMON_STATUS = f"{namespace}/status/daemon.json"
        REMOTE_CONTROL_REQUEST = f"{namespace}/daemon/control.json"
        REMOTE_CONTROL_ACK_DIR = f"{namespace}/daemon/acks"
    else:
        if host_id is not None:
            from local_agent.repository.host_routing import canonical_host_id

            canonical_host_id(host_id)
        REMOTE_TASKS_DIR = ".agent/tasks"
        REMOTE_RESULTS_DIR = ".agent/results"
        REMOTE_RUNS_DIR = ".agent/runs"
        REMOTE_DAEMON_STATUS = ".agent/status/daemon.json"
        REMOTE_CONTROL_REQUEST = ".agent/daemon/control.json"
        REMOTE_CONTROL_ACK_DIR = ".agent/daemon/acks"

CONTROL_ID_RE = re.compile(r"^[A-Za-z0-9._-]+$")

runtime = RuntimeExecutor(core)

_last_self_update_check = 0.0
_last_self_update_skip_reason: str | None = None
_last_remote_status = 0.0
_last_status_state: str | None = None
_daemon_lock_handle: Any | None = None
_current_task_id: str | None = None
_current_attempt_id: str | None = None
_current_task_digest: str | None = None
_current_progress: dict[str, Any] = {}
_remote_progress_publisher: CoalescingRemotePublisher | None = None


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def log(message: str) -> None:
    core.log(message)


def atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    atomic_write_text(path, json.dumps(payload, indent=2, ensure_ascii=False) + "\n")


class CoalescingRemotePublisher:
    """Publish the newest remote state per path without blocking local progress."""

    def __init__(self) -> None:
        self._condition = threading.Condition()
        self._pending: dict[str, tuple[dict[str, Any], str]] = {}
        self._active = False
        self._stopping = False
        self._thread = threading.Thread(
            target=self._run,
            daemon=True,
            name="local-agent-remote-progress",
        )
        self._thread.start()

    def submit(self, relative: str, payload: dict[str, Any], commit_message: str) -> None:
        with self._condition:
            if self._stopping:
                return
            self._pending.pop(relative, None)
            self._pending[relative] = (dict(payload), commit_message)
            self._condition.notify_all()

    def _run(self) -> None:
        while True:
            with self._condition:
                while not self._pending:
                    if self._stopping:
                        return
                    self._condition.wait()
                self._condition.wait(REMOTE_PROGRESS_COALESCE_SECONDS)
                if self._stopping:
                    self._pending.clear()
                    self._condition.notify_all()
                    return
                relative = next(iter(self._pending))
                payload, commit_message = self._pending.pop(relative)
                self._active = True
            try:
                publish_control_json(
                    relative,
                    payload,
                    commit_message=commit_message,
                    timeout=30,
                    attempts=1,
                )
            except Exception as exc:
                log(
                    f"remote progress publish failed for {relative}: "
                    f"{type(exc).__name__}: {exc}"
                )
            finally:
                with self._condition:
                    self._active = False
                    self._condition.notify_all()

    def flush(self, timeout: float = REMOTE_PROGRESS_FLUSH_SECONDS) -> bool:
        deadline = time.monotonic() + timeout
        with self._condition:
            while self._pending or self._active:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._condition.wait(remaining)
        return True

    def quiesce(self, timeout: float) -> bool:
        """Discard queued telemetry and wait boundedly for active Git publication."""
        deadline = time.monotonic() + timeout
        with self._condition:
            self._stopping = True
            self._pending.clear()
            self._condition.notify_all()
            while self._active:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._condition.wait(remaining)
        return True


def remote_progress_publisher() -> CoalescingRemotePublisher:
    global _remote_progress_publisher
    if _remote_progress_publisher is None:
        _remote_progress_publisher = CoalescingRemotePublisher()
    return _remote_progress_publisher


def flush_remote_progress() -> bool:
    publisher = _remote_progress_publisher
    return True if publisher is None else publisher.flush()


def quiesce_remote_progress(timeout: float = REMOTE_PROGRESS_SHUTDOWN_SECONDS) -> bool:
    publisher = _remote_progress_publisher
    return True if publisher is None else publisher.quiesce(timeout)


def shutdown_runtime_processes() -> None:
    """Stop task work, settle control Git state, then terminate remaining groups."""
    runtime.terminate_active_command()
    if not quiesce_remote_progress():
        log("remote progress publication did not quiesce before shutdown")
    terminate_active_processes(log)


def publish_control_json(
    relative: str,
    payload: dict[str, Any],
    *,
    commit_message: str,
    timeout: int = 180,
    attempts: int | None = None,
    log_commands: bool = False,
    ensure_remote: bool = False,
    post_pull_validate: Callable[[], None] | None = None,
) -> bool:
    """Publish control metadata; durable remote results get 3 bounded race attempts."""
    with core.CONTROL_GIT_LOCK:
        target = (core.CONTROL / relative).resolve()
        root = core.CONTROL.resolve()
        if root not in target.parents:
            raise ValueError(f"control path escapes repository: {relative!r}")

        with termination_critical_section():
            atomic_write_json(target, payload)
            add = core.process(
                ["git", "add", "--", relative], core.CONTROL, log_commands=log_commands
            )
            if add["exit_code"] != 0:
                raise RuntimeError(storage.git_failure_diagnostic(add))

            staged = core.process(
                ["git", "diff", "--cached", "--quiet", "--", relative],
                core.CONTROL,
                log_commands=log_commands,
            )
            if staged["exit_code"] == 0:
                if not ensure_remote:
                    return False
            elif staged["exit_code"] == 1:
                commit = core.process(
                    ["git", "commit", "-m", commit_message, "--", relative],
                    core.CONTROL,
                    log_commands=log_commands,
                )
                if commit["exit_code"] != 0:
                    raise RuntimeError(storage.git_failure_diagnostic(commit))
            else:
                raise RuntimeError(storage.git_failure_diagnostic(staged))

        return storage.publish_control_commit(
            core,
            timeout=timeout,
            attempts=(3 if ensure_remote else 2) if attempts is None else attempts,
            log_commands=log_commands,
            post_pull_validate=post_pull_validate,
        )


def load_task_file(path: Path) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"task path must be a regular file: {path.name}")
    size = path.stat().st_size
    if size > MAX_TASK_FILE_BYTES:
        raise ValueError(
            f"task file exceeds {MAX_TASK_FILE_BYTES} bytes: {path.name} has {size}"
        )
    task = json.loads(path.read_text(encoding="utf-8"))
    validate_task(task, tasks_root=path.parent)
    return task


def safe_control_directory(relative: str) -> Path:
    from local_agent.repository.host_routing import reject_host_namespace_symlinks

    reject_host_namespace_symlinks(core.CONTROL, f"{relative}/placeholder")
    path = (core.CONTROL / relative).resolve()
    root = core.CONTROL.resolve()
    if root not in path.parents:
        raise ValueError(f"control directory escapes repository: {relative!r}")
    path.mkdir(parents=True, exist_ok=True)
    return path


def self_revision() -> str | None:
    result = core.process(
        ["git", "rev-parse", "HEAD"], SELF_REPO, timeout=10, log_commands=False
    )
    return str(result["output"]).strip() if result["exit_code"] == 0 else None


def daemon_status_payload(state: str, **extra: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "version": 1,
        "daemon_version": DAEMON_VERSION,
        "state": state,
        "pid": os.getpid(),
        "updated_at": now_iso(),
        "self_revision": self_revision(),
        "poll_seconds": POLL_SECONDS,
        "self_update_seconds": SELF_UPDATE_INTERVAL,
        "command_timeout_default": TIMEOUTS.command_default,
        "command_timeout_max": TIMEOUTS.command_max,
        "idle_timeout_default": TIMEOUTS.idle_default,
        "idle_timeout_max": TIMEOUTS.idle_max,
        "task_timeout_default": TIMEOUTS.task_default,
        "task_timeout_max": TIMEOUTS.task_max,
        "memory_limit_mb_default": DEFAULT_MEMORY_LIMIT_MB,
        "memory_limit_mb_min": MIN_MEMORY_LIMIT_MB,
        "memory_limit_mb_max": MAX_MEMORY_LIMIT_MB,
        "current_task_id": _current_task_id,
        "current_attempt_id": _current_attempt_id,
        "current_task_digest": _current_task_digest,
    }
    payload.update(host_status_fields(STATE_DIR) | extra)
    return payload


def publish_daemon_status(
    state: str,
    *,
    force_remote: bool = False,
    remote_enabled: bool = True,
    **extra: Any,
) -> None:
    global _last_remote_status, _last_status_state

    payload = daemon_status_payload(state, **extra)
    atomic_write_json(LOCAL_STATUS_PATH, payload)
    now = time.monotonic()
    should_publish = (
        force_remote
        or state != _last_status_state
        or now - _last_remote_status >= REMOTE_HEARTBEAT_SECONDS
    )
    _last_status_state = state
    if not remote_enabled or not should_publish:
        return
    try:
        publish_control_json(
            REMOTE_DAEMON_STATUS,
            payload,
            commit_message=f"Agent daemon status: {state}",
        )
        _last_remote_status = now
    except Exception as exc:
        log(f"remote daemon status publish failed: {type(exc).__name__}: {exc}")


def task_claim_path(task_id: str) -> Path:
    digest = hashlib.sha256(task_id.encode("utf-8")).hexdigest()
    return CLAIMS_DIR / f"{digest}.json"


def claim_task(task: dict[str, Any]) -> dict[str, Any] | None:
    validate_task(task)
    task_id = str(task["id"])
    digest = task_digest(task)
    attempt_id = secrets.token_hex(12)
    payload = {
        "id": task_id,
        "task_digest": digest,
        "attempt_id": attempt_id,
        "pid": os.getpid(),
        "daemon_version": DAEMON_VERSION,
        "started_at": now_iso(),
    }
    CLAIMS_DIR.mkdir(parents=True, exist_ok=True)
    path = task_claim_path(task_id)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            existing = {}
        if existing.get("task_digest") not in (None, digest):
            log(f"task id payload mismatch; refusing execution: {task_id}")
        else:
            log(f"task already claimed; refusing replay: {task_id}")
        return None

    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, ensure_ascii=False)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    fsync_directory(CLAIMS_DIR)
    log(f"claimed task {task_id} attempt={attempt_id}")
    return payload


def release_task_claim(task_id: str) -> None:
    try:
        task_claim_path(task_id).unlink()
        fsync_directory(CLAIMS_DIR)
        log(f"released task claim {task_id}")
    except FileNotFoundError:
        pass


def result_spool_path(task_id: str) -> Path:
    digest = hashlib.sha256(task_id.encode("utf-8")).hexdigest()
    return RESULT_SPOOL_DIR / f"{digest}.json"


def persist_result_spool(task_id: str, result: dict[str, Any]) -> Path:
    """Durably preserve a final result before attempting network publication."""
    path = result_spool_path(task_id)
    atomic_write_json(
        path,
        {
            "version": 1,
            "task_id": task_id,
            "persisted_at": now_iso(),
            "result": result,
        },
    )
    return path


def read_result_spool(task_id: str) -> dict[str, Any] | None:
    path = result_spool_path(task_id)
    if not path.exists():
        return None
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or payload.get("version") != 1:
        raise ValueError(f"invalid result spool format for {task_id}")
    if payload.get("task_id") != task_id or not isinstance(payload.get("result"), dict):
        raise ValueError(f"result spool identity mismatch for {task_id}")
    return payload["result"]


def discard_result_spool(task_id: str) -> None:
    path = result_spool_path(task_id)
    try:
        path.unlink()
        fsync_directory(path.parent)
    except FileNotFoundError:
        pass


def publish_durable_result(task_id: str, result: dict[str, Any]) -> None:
    """Publish an already-spooled result and acknowledge its durable delivery."""
    if read_result_spool(task_id) is None:
        raise RuntimeError(f"result is not durably spooled for {task_id}")
    core.publish_result(task_id, result)
    discard_result_spool(task_id)


def has_pending_publications() -> bool:
    return any(RESULT_SPOOL_DIR.glob("*.json")) or any(CLAIMS_DIR.glob("*.json"))


def clear_current_task(task_id: str) -> None:
    global _current_task_id, _current_attempt_id, _current_task_digest, _current_progress
    if _current_task_id not in (None, task_id):
        return
    _current_task_id = None
    _current_attempt_id = None
    _current_task_digest = None
    _current_progress = {}


def invalid_task_result(task_id: str, error: Exception) -> dict[str, Any]:
    failure_reason = (
        "invalid_dedupe_key" if isinstance(error, InvalidDedupeMetadata) else "invalid_task_file"
    )
    return {
        "id": task_id,
        "status": "failed",
        "failure_reason": failure_reason,
        "started_at": None,
        "finished_at": now_iso(),
        "daemon_version": DAEMON_VERSION,
        "error": f"{type(error).__name__}: {error}",
    }


def recover_invalid_task_files() -> None:
    tasks_dir = safe_control_directory(REMOTE_TASKS_DIR)
    results_dir = safe_control_directory(REMOTE_RESULTS_DIR)

    for path in sorted(tasks_dir.glob("*.json")):
        rejection_id = path.stem
        rejection_result = results_dir / f"{rejection_id}.json"
        if rejection_result.exists():
            continue
        try:
            load_task_file(path)
        except Exception as exc:
            log(f"rejecting invalid task file {path.name}: {type(exc).__name__}: {exc}")
            result = invalid_task_result(rejection_id, exc)
            try:
                core.publish_result(rejection_id, result)
            except Exception as publish_exc:
                log(f"failed to publish invalid-task result for {rejection_id}: {publish_exc}")
                continue
            publish_run_state(
                rejection_id,
                {
                    "event": "invalid_task_rejected",
                    "status": "failed",
                    "failure_reason": result["failure_reason"],
                    "updated_at": now_iso(),
                },
                force_remote=True,
            )


def pending_tasks() -> list[tuple[Path, dict[str, Any]]]:
    tasks_dir = safe_control_directory(REMOTE_TASKS_DIR)
    results_dir = safe_control_directory(REMOTE_RESULTS_DIR)
    pending: list[tuple[Path, dict[str, Any]]] = []

    for path in sorted(tasks_dir.glob("*.json")):
        task_id_hint = path.stem
        if (results_dir / f"{task_id_hint}.json").exists():
            continue
        try:
            task = load_task_file(path)
            task_id = str(task["id"])
        except Exception as exc:
            log(f"invalid task file {path.name}: {type(exc).__name__}: {exc}")
            continue

        result_path = results_dir / f"{task_id}.json"
        if result_path.exists():
            try:
                result = json.loads(result_path.read_text(encoding="utf-8"))
            except Exception:
                result = {}
            old_digest = result.get("task_digest")
            new_digest = task_digest(task)
            if old_digest and old_digest != new_digest:
                log(f"task id reuse detected after result; refusing: {task_id}")
            continue
        if task_claim_path(task_id).exists():
            log(f"task already claimed; skipping replay: {task_id}")
            continue
        pending.append((path, task))
    return pending


def interrupted_result(task_id: str, claim: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": task_id,
        "status": "failed",
        "failure_reason": "interrupted_previous_attempt",
        "task_digest": claim.get("task_digest"),
        "attempt_id": claim.get("attempt_id"),
        "daemon_version": claim.get("daemon_version"),
        "started_at": claim.get("started_at"),
        "finished_at": now_iso(),
        "error": (
            "Previous daemon instance ended while this task was claimed. "
            "Automatic replay was blocked."
        ),
    }


def _task_for_claim_path(path: Path) -> dict[str, Any] | None:
    tasks_dir = safe_control_directory(REMOTE_TASKS_DIR)
    for task_path in sorted(tasks_dir.glob("*.json")):
        try:
            task = load_task_file(task_path)
            task_id = str(task["id"])
        except Exception:
            continue
        if task_claim_path(task_id).name == path.name:
            return task
    return None


def _quarantine_corrupt_claim(path: Path) -> Path:
    CORRUPT_CLAIMS_DIR.mkdir(parents=True, exist_ok=True)
    target = CORRUPT_CLAIMS_DIR / f"{path.stem}.{time.time_ns()}.json"
    os.replace(path, target)
    fsync_directory(path.parent)
    fsync_directory(target.parent)
    return target


def corrupt_claim_result(task: dict[str, Any], error: Exception) -> dict[str, Any]:
    task_id = str(task["id"])
    return {
        "id": task_id,
        "status": "failed",
        "failure_reason": "corrupt_claim_state",
        "task_digest": task_digest(task),
        "attempt_id": None,
        "daemon_version": DAEMON_VERSION,
        "started_at": None,
        "finished_at": now_iso(),
        "error": (
            f"Durable claim state could not be decoded ({type(error).__name__}: {error}). "
            "Automatic replay was blocked and the corrupt claim was quarantined."
        ),
    }


def recover_stale_claims() -> None:
    CLAIMS_DIR.mkdir(parents=True, exist_ok=True)
    results_dir = safe_control_directory(REMOTE_RESULTS_DIR)
    for path in sorted(CLAIMS_DIR.glob("*.json")):
        try:
            claim = json.loads(path.read_text(encoding="utf-8"))
            task_id = str(claim["id"])
        except Exception as exc:
            task = _task_for_claim_path(path)
            if task is None:
                quarantined = _quarantine_corrupt_claim(path)
                log(
                    f"quarantined unmatched corrupt claim {path.name} -> "
                    f"{quarantined.name}: {type(exc).__name__}: {exc}"
                )
                continue

            task_id = str(task["id"])
            try:
                result = read_result_spool(task_id)
            except Exception as spool_exc:
                log(
                    f"invalid result spool retained for {task_id}: "
                    f"{type(spool_exc).__name__}: {spool_exc}"
                )
                continue
            if result is None:
                result = corrupt_claim_result(task, exc)
                try:
                    persist_result_spool(task_id, result)
                except Exception as persist_exc:
                    log(f"failed to persist corrupt-claim result for {task_id}: {persist_exc}")
                    continue
            log(f"recovering corrupt claim without replay: {task_id}")
            try:
                publish_durable_result(task_id, result)
            except Exception as publish_exc:
                log(f"failed to publish corrupt-claim result for {task_id}: {publish_exc}")
                continue
            publish_run_state(
                task_id,
                {
                    "event": "recovered_corrupt_claim",
                    "status": "failed",
                    "failure_reason": "corrupt_claim_state",
                    "attempt_id": None,
                    "task_digest": result.get("task_digest"),
                    "updated_at": now_iso(),
                },
                force_remote=True,
            )
            quarantined = _quarantine_corrupt_claim(path)
            log(f"quarantined corrupt claim for {task_id}: {quarantined.name}")
            clear_current_task(task_id)
            continue

        try:
            spooled_result = read_result_spool(task_id)
        except Exception as exc:
            log(
                f"invalid result spool retained for {task_id}: {type(exc).__name__}: {exc}"
            )
            continue
        if spooled_result is not None:
            log(f"retrying durable result publication without replay: {task_id}")
            try:
                publish_durable_result(task_id, spooled_result)
            except Exception as exc:
                log(f"failed to republish durable result for {task_id}: {exc}")
                continue
            publish_run_state(
                task_id,
                {
                    "event": "result_republished",
                    "status": spooled_result.get("status"),
                    "failure_reason": spooled_result.get("failure_reason"),
                    "attempt_id": claim.get("attempt_id"),
                    "task_digest": claim.get("task_digest"),
                    "updated_at": now_iso(),
                },
                force_remote=True,
            )
            release_task_claim(task_id)
            clear_current_task(task_id)
            continue

        result_path = results_dir / f"{task_id}.json"
        if result_path.exists():
            try:
                result = json.loads(result_path.read_text(encoding="utf-8"))
            except Exception:
                result = interrupted_result(task_id, claim)
        else:
            result = interrupted_result(task_id, claim)

        log(f"recovering interrupted task without replay: {task_id}")
        try:
            persist_result_spool(task_id, result)
            publish_durable_result(task_id, result)
        except Exception as exc:
            log(f"failed to publish interrupted result for {task_id}: {exc}")
            continue
        publish_run_state(
            task_id,
            {
                "event": "recovered_interrupted_attempt",
                "status": "failed",
                "failure_reason": result.get("failure_reason"),
                "attempt_id": claim.get("attempt_id"),
                "task_digest": claim.get("task_digest"),
                "updated_at": now_iso(),
            },
            force_remote=True,
        )
        release_task_claim(task_id)
        clear_current_task(task_id)


def acquire_daemon_lock() -> Any:
    require_completed_installation(STATE_DIR)
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    handle = DAEMON_LOCK_PATH.open("a+", encoding="utf-8")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        log("another local-agent daemon is already running; exiting")
        raise SystemExit(0)
    handle.seek(0)
    handle.truncate()
    handle.write(json.dumps({"pid": os.getpid(), "started_at": now_iso()}) + "\n")
    handle.flush()
    os.fsync(handle.fileno())
    return handle


def _git(args: list[str], timeout: int = 60) -> subprocess.CompletedProcess[str]:
    result = storage.run_git_with_network_retry(
        core, args, SELF_REPO, timeout=timeout, log_commands=False
    )
    return subprocess.CompletedProcess(
        args=args,
        returncode=int(result["exit_code"]),
        stdout=str(result.get("output", "")),
        stderr=None,
    )


def tracked_self_repo_clean() -> bool:
    try:
        status = _git(["git", "status", "--porcelain", "--untracked-files=normal"])
    except (OSError, subprocess.TimeoutExpired):
        return False
    return status.returncode == 0 and not status.stdout.strip()


def self_repo_on_main_branch() -> bool:
    try:
        branch = _git(["git", "symbolic-ref", "--quiet", "--short", "HEAD"])
    except (OSError, subprocess.TimeoutExpired):
        return False
    return branch.returncode == 0 and branch.stdout.strip() == SELF_BRANCH


def _read_rejected_update() -> dict[str, Any]:
    try:
        return json.loads(REJECTED_UPDATE_PATH.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def _remember_rejected_update(remote_sha: str, error: str) -> None:
    atomic_write_json(
        REJECTED_UPDATE_PATH,
        {
            "sha": remote_sha,
            "rejected_at": now_iso(),
            "reason": "validation_failed",
            "error": core.bounded(error, 4000),
        },
    )


def _validate_installed_update() -> tuple[bool, str]:
    commands = [
        [sys.executable, "scripts/verify.py", "--only", "compile"],
        [sys.executable, "-m", "unittest", "discover", "-q"],
    ]
    with tempfile.TemporaryDirectory(prefix="local-agent-update-validation-") as home:
        validation_env = dict(core.ENV)
        for name in (LEASE_FDS_ENV, LEASE_KEYS_DIGEST_ENV, RESOURCE_LEASE_FDS_ENV):
            validation_env.pop(name, None)
        validation_env["HOME"] = home
        for command in commands:
            result = core.process(
                command,
                SELF_REPO,
                environment=validation_env,
                timeout=SELF_UPDATE_VALIDATION_TIMEOUT_SECONDS,
                log_commands=False,
            )
            if result["exit_code"] != 0:
                return False, str(result.get("output", "")).strip()
    return True, ""


def recover_interrupted_self_update() -> tuple[bool, str]:
    """Revalidate an interrupted installed checkout before clearing its journal."""
    with installation_transaction(STATE_DIR) as acquired:
        if not acquired:
            return False, "installation_lock_busy"
        if not installation_pending(STATE_DIR):
            return True, "not_pending"

        try:
            journal = read_pending_installation(STATE_DIR)
        except RuntimeError as exc:
            return False, str(exc)

        if not self_repo_on_main_branch():
            return False, "installed checkout is not on main"
        if not tracked_self_repo_clean():
            return False, "installed checkout is not clean"

        current = self_revision()
        if current is None:
            return False, "cannot resolve installed checkout revision"
        current = current.lower()
        original = journal["original_revision"]
        candidate = journal["candidate_revision"]

        if current != original:
            try:
                ancestor = _git(
                    ["git", "merge-base", "--is-ancestor", candidate, current],
                    timeout=10,
                )
            except (OSError, subprocess.TimeoutExpired) as exc:
                return False, f"cannot verify interrupted candidate ancestry: {exc}"
            if ancestor.returncode != 0:
                return False, (
                    "installed checkout is neither the recorded original revision "
                    "nor a descendant of the interrupted candidate"
                )

        valid, error = _validate_installed_update()
        if not valid:
            return False, "installed checkout validation failed: " + core.bounded(error, 2000)

        finish_installation(STATE_DIR)
        REJECTED_UPDATE_PATH.unlink(missing_ok=True)
        return True, "validated"


def restart_self(reason: str) -> None:
    publish_daemon_status("restarting", force_remote=True, reason=reason)
    log(f"restarting daemon: {reason}")
    for name in (LEASE_FDS_ENV, LEASE_KEYS_DIGEST_ENV, RESOURCE_LEASE_FDS_ENV):
        os.environ.pop(name, None)
        core.ENV.pop(name, None)
    try:
        os.execv(sys.executable, [sys.executable, str(SELF_REPO / "agentd.py")])
    except OSError as exc:
        log(f"exec restart failed; asking launchd to restart: {exc}")
        raise SystemExit(75) from exc


def maybe_self_update(force: bool = False) -> bool:
    with installation_transaction(STATE_DIR) as acquired:
        if not acquired:
            return False
        require_completed_installation(STATE_DIR)
        return _self_update_transaction(force=force)


def _report_self_update_eligibility(reason: str | None) -> None:
    """Log only transitions in eligibility, not every periodic poll.

    This is process-local diagnostics only. It never changes the update
    interval, Git checkout, rollout authority, or any self-update gate.
    """
    global _last_self_update_skip_reason
    if reason == _last_self_update_skip_reason:
        return
    previous = _last_self_update_skip_reason
    _last_self_update_skip_reason = reason
    if reason is None:
        if previous is not None:
            log("self-update checks resumed: local checkout eligible")
    elif reason == "no_git":
        log("self-update disabled: installed checkout has no .git metadata")
    elif reason == "not_main":
        log(f"self-update disabled: checkout is not on {SELF_BRANCH} (pinned source)")
    elif reason == "dirty":
        log("self-update paused: checkout is not clean")
    elif reason == "non_fast_forward":
        log(f"self-update paused: remote {SELF_BRANCH} is not a fast-forward")


def _self_update_transaction(*, force: bool) -> bool:
    global _last_self_update_check
    now = time.monotonic()
    if not force and now - _last_self_update_check < SELF_UPDATE_INTERVAL:
        return False
    _last_self_update_check = now
    if not (SELF_REPO / ".git").exists():
        _report_self_update_eligibility("no_git")
        return False
    if not self_repo_on_main_branch():
        _report_self_update_eligibility("not_main")
        return False
    if not tracked_self_repo_clean():
        _report_self_update_eligibility("dirty")
        return False

    try:
        fetch = _git(["git", "fetch", "--quiet", "origin", SELF_BRANCH])
    except (OSError, subprocess.TimeoutExpired) as exc:
        _report_self_update_eligibility(None)
        log(f"self-update fetch failed: {exc}")
        return False
    if fetch.returncode != 0:
        _report_self_update_eligibility(None)
        log(f"self-update fetch failed: {core.bounded(fetch.stdout.strip(), 2000)}")
        return False

    local = _git(["git", "rev-parse", "HEAD"])
    remote = _git(["git", "rev-parse", f"origin/{SELF_BRANCH}"])
    if local.returncode != 0 or remote.returncode != 0:
        _report_self_update_eligibility(None)
        return False
    local_sha = local.stdout.strip()
    remote_sha = remote.stdout.strip()
    if local_sha == remote_sha:
        _report_self_update_eligibility(None)
        REJECTED_UPDATE_PATH.unlink(missing_ok=True)
        return False
    if _read_rejected_update().get("sha") == remote_sha:
        _report_self_update_eligibility(None)
        return False

    ancestor = _git(["git", "merge-base", "--is-ancestor", local_sha, remote_sha])
    if ancestor.returncode != 0:
        _report_self_update_eligibility("non_fast_forward")
        return False

    _report_self_update_eligibility(None)
    log(f"self-update available {local_sha[:9]} -> {remote_sha[:9]}")
    publish_daemon_status(
        "self_updating",
        force_remote=True,
        current_revision=local_sha,
        candidate_revision=remote_sha,
    )
    begin_installation(STATE_DIR, local_sha, remote_sha)
    try:
        install = _git(["git", "merge", "--ff-only", "--quiet", remote_sha], timeout=120)
        if install.returncode != 0:
            valid, error = False, str(install.stdout).strip()
        else:
            valid, error = _validate_installed_update()
    except Exception as exc:
        valid, error = False, f"{type(exc).__name__}: {exc}"
    if not valid:
        log("self-update validation failed; rolling back: " + core.bounded(error, 2000))
        try:
            rollback = _git(["git", "reset", "--hard", local_sha], timeout=60)
        except Exception as exc:
            log(f"CRITICAL: self-update rollback failed: {type(exc).__name__}: {exc}")
            raise SystemExit(75) from exc
        if rollback.returncode == 0:
            _remember_rejected_update(remote_sha, error)
            finish_installation(STATE_DIR)
        else:
            log("CRITICAL: self-update rollback failed")
            raise SystemExit(75)
        return False

    REJECTED_UPDATE_PATH.unlink(missing_ok=True)
    finish_installation(STATE_DIR)
    log(f"self-update installed {remote_sha[:9]}")
    restart_self("self_update")
    return True


def valid_control_id(control_id: str) -> bool:
    return bool(control_id) and len(control_id) <= 120 and CONTROL_ID_RE.fullmatch(control_id) is not None


def control_ack_relative_path(control_id: str) -> str:
    if not valid_control_id(control_id):
        raise ValueError(f"invalid daemon control id: {control_id!r}")
    relative = f"{REMOTE_CONTROL_ACK_DIR}/{control_id}.json"
    target = (core.CONTROL / relative).resolve()
    ack_root = (core.CONTROL / REMOTE_CONTROL_ACK_DIR).resolve()
    if ack_root not in target.parents:
        raise ValueError(f"control ACK path escapes ACK directory: {control_id!r}")
    return relative


def _control_ack_path(control_id: str) -> Path:
    return core.CONTROL / control_ack_relative_path(control_id)


def control_ack_published(control_id: str) -> bool:
    relative = control_ack_relative_path(control_id)
    result = core.process(
        ["git", "ls-tree", "--name-only", f"origin/{core.CONTROL_BRANCH}", "--", relative],
        core.CONTROL,
        timeout=30,
        log_commands=False,
    )
    if result["exit_code"] != 0:
        raise RuntimeError(storage.git_failure_diagnostic(result))
    return relative in str(result.get("output", "")).splitlines()


def publish_control_ack(control_id: str, action: str, status: str, **extra: Any) -> None:
    relative = control_ack_relative_path(control_id)
    payload = {
        "id": control_id,
        "action": action,
        "status": status,
        "daemon_version": DAEMON_VERSION,
        "pid": os.getpid(),
        "updated_at": now_iso(),
    }
    payload.update(extra)
    publish_control_json(
        relative,
        payload,
        commit_message=f"Agent daemon control ack: {control_id}",
    )


def handle_control_request(
    *, status_extra: dict[str, Any] | None = None,
    validated_request: dict[str, Any] | None = None,
) -> None:
    # Callers handling a host-owned mailbox must pass an already target-validated
    # immutable snapshot, not reread an untrusted file after authorization.
    if validated_request is None:
        path = core.CONTROL / REMOTE_CONTROL_REQUEST
        if not path.exists():
            return
        try:
            request = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            log(f"invalid daemon control request: {exc}")
            return
    else:
        request = validated_request
    try:
        control_id = str(request["id"])
        action = str(request["action"])
    except Exception as exc:
        log(f"invalid daemon control request: {exc}")
        return
    if not valid_control_id(control_id):
        log(f"invalid daemon control id: {control_id!r}")
        return
    try:
        if control_ack_published(control_id):
            return
    except Exception as exc:
        log(f"control ACK verification failed for {control_id}: {type(exc).__name__}: {exc}")
        return

    if action == "restart":
        publish_control_ack(control_id, action, "accepted")
        restart_self(f"remote_control:{control_id}")
    elif action == "self_update":
        publish_control_ack(control_id, action, "accepted")
        if not maybe_self_update(force=True):
            publish_control_ack(control_id, action, "completed", result="no_update")
    elif action == "status":
        publish_daemon_status("idle", force_remote=True, **(status_extra or {}))
        publish_control_ack(control_id, action, "completed")
    else:
        publish_control_ack(control_id, action, "rejected", error="unsupported_action")


def publish_run_state(
    task_id: str,
    payload: dict[str, Any],
    *,
    force_remote: bool,
    asynchronous_remote: bool = False,
) -> None:
    state = dict(payload)
    state.setdefault("task_id", task_id)
    state.setdefault("daemon_version", DAEMON_VERSION)
    state.setdefault("pid", os.getpid())
    state.setdefault("updated_at", now_iso())
    atomic_write_json(LOCAL_RUNS_DIR / f"{task_id}.json", state)
    if not force_remote:
        return
    relative = f"{REMOTE_RUNS_DIR}/{task_id}.json"
    commit_message = f"Agent progress: {task_id}"
    if asynchronous_remote:
        remote_progress_publisher().submit(relative, state, commit_message)
        return
    try:
        publish_control_json(
            relative,
            state,
            commit_message=commit_message,
            timeout=30,
            attempts=1,
        )
    except Exception as exc:
        log(f"remote run state publish failed for {task_id}: {exc}")


def make_progress_callback(
    task_id: str,
    attempt_id: str,
    digest: str,
    *,
    remote_daemon_status: bool = True,
):
    last_remote = 0.0
    last_remote_phase: str | None = None
    last_remote_stage: str | None = None

    def progress(event: dict[str, Any]) -> None:
        nonlocal last_remote, last_remote_phase, last_remote_stage
        global _current_progress
        enriched = dict(event)
        enriched.update(
            {
                "task_id": task_id,
                "attempt_id": attempt_id,
                "task_digest": digest,
                "daemon_version": DAEMON_VERSION,
                "daemon_pid": os.getpid(),
                "updated_at": now_iso(),
            }
        )
        _current_progress = enriched
        now = time.monotonic()
        event_name = str(event.get("event", ""))
        phase = str(event.get("stage_phase", event.get("phase", "")))
        stage_name = str(event.get("stage_name", ""))

        force_remote = event_name in {"task_started", "task_finished"}
        if event_name in {"command_started", "stage_progress"}:
            first_command = last_remote_phase is None
            phase_changed = bool(last_remote_phase) and phase != last_remote_phase
            stage_changed = bool(stage_name) and stage_name != last_remote_stage
            progress_due = now - last_remote >= RUN_PROGRESS_SECONDS
            force_remote = first_command or phase_changed or stage_changed or progress_due
        if event_name == "command_finished":
            if int(event.get("exit_code", 0)) != 0:
                force_remote = True
            elif float(event.get("elapsed_seconds", 0.0)) >= RUN_PROGRESS_SECONDS:
                force_remote = True
        if event_name == "command_heartbeat" and now - last_remote >= RUN_HEARTBEAT_SECONDS:
            force_remote = True

        publish_run_state(
            task_id,
            enriched,
            force_remote=force_remote,
            asynchronous_remote=True,
        )
        if force_remote:
            last_remote = now
            if phase:
                last_remote_phase = phase
            if stage_name:
                last_remote_stage = stage_name

        status_extra: dict[str, Any] = {"progress": enriched}
        if enriched.get("last_progress_at") is not None:
            status_extra["last_progress_at"] = enriched["last_progress_at"]
        if enriched.get("last_progress_message") is not None:
            status_extra["last_progress_message"] = enriched["last_progress_message"]
        publish_daemon_status(
            "running",
            force_remote=False,
            remote_enabled=remote_daemon_status,
            **status_extra,
        )

    return progress


def shutdown_handler(signum: int, _frame: Any) -> None:
    if defer_termination(signum):
        return
    log(f"received signal {signum}; terminating active processes")
    shutdown_runtime_processes()
    raise SystemExit(128 + signum)


def install_signal_handlers() -> None:
    signal.signal(signal.SIGTERM, shutdown_handler)
    signal.signal(signal.SIGINT, shutdown_handler)


def execute_task(
    task: dict[str, Any],
    *,
    remote_daemon_status: bool = True,
    remote_result_published: bool = True,
) -> str:
    """Preserve Mac release admission before any legacy claim or task effect.

    Parallel/serial workers already hold a turn-wide lease; this closes the
    direct single-repository daemon entrypoint without enabling a controller.
    """
    with macos_task_admission(STATE_DIR):
        return _execute_task_admitted(
            task,
            remote_daemon_status=remote_daemon_status,
            remote_result_published=remote_result_published,
        )


def _execute_task_admitted(
    task: dict[str, Any],
    *,
    remote_daemon_status: bool = True,
    remote_result_published: bool = True,
) -> str:
    global _current_task_id, _current_attempt_id, _current_task_digest, _current_progress
    claim = claim_task(task)
    if claim is None:
        return "not_claimed"

    task_id = str(task["id"])
    _current_task_id = task_id
    _current_attempt_id = str(claim["attempt_id"])
    _current_task_digest = str(claim["task_digest"])
    _current_progress = {}
    progress = make_progress_callback(
        task_id,
        _current_attempt_id,
        _current_task_digest,
        remote_daemon_status=remote_daemon_status,
    )
    publish_daemon_status(
        "running",
        force_remote=remote_daemon_status,
        remote_enabled=remote_daemon_status,
    )

    try:
        result = runtime.process_task(task, progress=progress)
    except Exception as exc:
        result = {
            "id": task_id,
            "status": "failed",
            "failure_reason": "daemon_exception",
            "task_digest": _current_task_digest,
            "started_at": claim.get("started_at"),
            "finished_at": now_iso(),
            "error": f"{type(exc).__name__}: {exc}",
            "traceback": traceback.format_exc(),
        }
    result["attempt_id"] = _current_attempt_id
    result["daemon_version"] = DAEMON_VERSION
    result.setdefault("task_digest", _current_task_digest)

    if not flush_remote_progress():
        log(f"remote progress did not flush before final result for {task_id}")

    try:
        persist_result_spool(task_id, result)
    except Exception as exc:
        log(f"result persistence failed for {task_id}: {exc}")
        publish_daemon_status(
            "result_persistence_failed",
            force_remote=remote_daemon_status,
            remote_enabled=remote_daemon_status,
            error=f"{type(exc).__name__}: {exc}",
        )
        return "publication_pending"

    try:
        publish_durable_result(task_id, result)
    except Exception as exc:
        log(f"result publish failed for {task_id}: {exc}")
        publish_daemon_status(
            "publication_pending",
            force_remote=remote_daemon_status,
            remote_enabled=remote_daemon_status,
            error=f"{type(exc).__name__}: {exc}",
        )
        return "publication_pending"

    publish_run_state(
        task_id,
        {
            "event": "result_published",
            "status": result.get("status"),
            "failure_reason": result.get("failure_reason"),
            "attempt_id": _current_attempt_id,
            "task_digest": _current_task_digest,
            "finished_at": result.get("finished_at"),
            "updated_at": now_iso(),
        },
        force_remote=remote_result_published,
    )
    release_task_claim(task_id)
    clear_current_task(task_id)
    publish_daemon_status(
        "idle",
        force_remote=remote_daemon_status,
        remote_enabled=remote_daemon_status,
    )
    return "published"


def main() -> None:
    global _daemon_lock_handle
    _daemon_lock_handle = acquire_daemon_lock()
    install_signal_handlers()
    log(
        f"Local Agent daemon v{DAEMON_VERSION} starting; "
        f"command_timeout_default={TIMEOUTS.command_default}s "
        f"command_timeout_max={TIMEOUTS.command_max}s "
        f"idle_timeout_default={TIMEOUTS.idle_default}s "
        f"idle_timeout_max={TIMEOUTS.idle_max}s "
        f"task_timeout_default={TIMEOUTS.task_default}s "
        f"task_timeout_max={TIMEOUTS.task_max}s "
        f"memory_limit={DEFAULT_MEMORY_LIMIT_MB}MiB "
        f"self_update={SELF_UPDATE_INTERVAL}s"
    )

    core.sync_control()
    recover_stale_claims()
    recover_invalid_task_files()
    publish_daemon_status("idle", force_remote=True)

    while True:
        try:
            core.sync_control()
            recover_stale_claims()
            recover_invalid_task_files()
            handle_control_request()
            maybe_self_update()
            tasks = pending_tasks()
            if not tasks and has_pending_publications():
                publish_daemon_status("publication_pending")
            elif not tasks:
                log("no pending tasks")
                publish_daemon_status("idle")
            for _, task in tasks:
                execute_task(task)
        except SystemExit:
            raise
        except Exception as exc:
            log(f"poll loop error: {type(exc).__name__}: {exc}")
            traceback.print_exc()
            publish_daemon_status(
                "error",
                force_remote=True,
                error=f"{type(exc).__name__}: {exc}",
            )
        time.sleep(POLL_SECONDS)


def multirepo_registry_path() -> Path:
    return STATE_DIR / "repositories.json"


def dispatch_multirepo_if_configured() -> bool:
    registry = multirepo_registry_path()
    if not registry.is_file():
        return False
    supervisor = SELF_REPO / "agent_multirepo.py"
    if not supervisor.is_file():
        raise RuntimeError(f"multi-repository supervisor missing: {supervisor}")
    log(f"repository registry detected; dispatching supervisor registry={registry}")
    os.execv(
        sys.executable,
        [sys.executable, str(supervisor), "--registry", str(registry)],
    )
    return True


def run() -> None:
    if not dispatch_multirepo_if_configured():
        main()


if __name__ == "__main__":
    run()
