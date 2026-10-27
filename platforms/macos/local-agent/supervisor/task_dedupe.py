from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import local_agent.foundation.core as core
from local_agent.foundation.process import atomic_write_text
from local_agent.runtime.task_contract import (
    InvalidDedupeMetadata,
    idle_timeout_for,
    memory_limit_for,
    task_dedupe_identity,
    task_digest,
    task_timeout_for,
)

RECENT_COMPLETION_TTL_SECONDS = 30 * 60
ADMISSION_GRACE_SECONDS = 10 * 60

PendingTask = tuple[Path, dict[str, Any]]


@dataclass(frozen=True)
class SuppressedTask:
    item: PendingTask
    duplicate_of: str
    queue_key: str
    execution_fingerprint: str
    reason: str


@dataclass(frozen=True)
class InvalidTask:
    item: PendingTask
    error: str
    reason: str = "invalid_dedupe_key"


@dataclass(frozen=True)
class QueuePlan:
    candidates: tuple[PendingTask, ...]
    suppressed: tuple[SuppressedTask, ...]
    invalid: tuple[InvalidTask, ...]


def _serialized(value: Any) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")


def _structured_execution_steps(
    task: dict[str, Any],
    field: str,
    *,
    command_timeout: int,
) -> list[dict[str, Any]]:
    steps: list[dict[str, Any]] = []
    for item in task.get(field, []):
        if not isinstance(item, dict):
            continue
        command = item.get("command")
        if not isinstance(command, str):
            continue
        steps.append({
            "command": command,
            "timeout": item.get("timeout", command_timeout),
        })
    return steps


def execution_contract(task: dict[str, Any]) -> dict[str, Any]:
    """Return deterministic task effects plus limits that can change completion."""
    command_timeout = core.command_timeout_for(task)
    return {
        "agent_binding": task.get("agent_binding"),
        "mode": task.get("mode", "commands"),
        "work_branch": task.get("work_branch", "main"),
        "allow_write": task.get("allow_write", False),
        "resources": task.get("resources", []),
        "patch": task.get("patch"),
        "writes": task.get("writes", []),
        "deletes": task.get("deletes", []),
        "commands": task.get("commands", []),
        "verify_commands": task.get("verify_commands", []),
        "steps": _structured_execution_steps(
            task,
            "steps",
            command_timeout=command_timeout,
        ),
        "verify_steps": _structured_execution_steps(
            task,
            "verify_steps",
            command_timeout=command_timeout,
        ),
        "command_timeout": command_timeout,
        "idle_timeout": idle_timeout_for(task),
        "task_timeout": task_timeout_for(task),
        "memory_limit_mb": memory_limit_for(task),
    }


def execution_fingerprint(task: dict[str, Any]) -> str:
    """Fingerprint task effects independently of id, stage labels and output tuning."""
    return hashlib.sha256(_serialized(execution_contract(task))).hexdigest()


def queue_key(task: dict[str, Any]) -> str:
    """Return branch-scoped intent identity or fall back to the exact effect fingerprint."""
    explicit, _ = task_dedupe_identity(task)
    if explicit is not None:
        branch = str(task.get("work_branch", "main"))
        return f"intent:{branch}:{explicit}"
    return f"effect:{execution_fingerprint(task)}"


def _receipt_path(state_dir: Path, key: str) -> Path:
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
    return state_dir / "task-dedupe" / f"{digest}.json"


def _claim_path(state_dir: Path, task_id: str) -> Path:
    digest = hashlib.sha256(task_id.encode("utf-8")).hexdigest()
    return state_dir / "claims" / f"{digest}.json"


def _published_run_matches_receipt(state_dir: Path, payload: dict[str, Any]) -> bool:
    task_id = payload.get("task_id")
    expected_digest = payload.get("task_digest")
    if not isinstance(task_id, str) or not task_id or not isinstance(expected_digest, str):
        return False
    try:
        run = json.loads((state_dir / "runs" / f"{task_id}.json").read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return False
    return (
        isinstance(run, dict)
        and run.get("event") == "result_published"
        and run.get("task_digest") == expected_digest
    )


def _read_receipt(
    state_dir: Path,
    key: str,
    *,
    now_epoch: float,
) -> dict[str, Any] | None:
    path = _receipt_path(state_dir, key)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (json.JSONDecodeError, OSError):
        path.unlink(missing_ok=True)
        return None

    if not isinstance(payload, dict) or payload.get("version") != 1:
        path.unlink(missing_ok=True)
        return None
    expires_at = payload.get("expires_at_epoch")
    if not isinstance(expires_at, (int, float)) or isinstance(expires_at, bool):
        path.unlink(missing_ok=True)
        return None
    if float(expires_at) <= now_epoch:
        path.unlink(missing_ok=True)
        return None
    task_id = payload.get("task_id")
    if not isinstance(task_id, str) or not task_id:
        path.unlink(missing_ok=True)
        return None

    state = payload.get("state")
    if state == "admitted":
        if not _claim_path(state_dir, task_id).exists():
            if _published_run_matches_receipt(state_dir, payload):
                payload["state"] = "completed"
                payload["outcome"] = "published"
                payload["reconciled_from_published_run"] = True
                payload["reconciliation_reason"] = "published_run_after_claim_release"
                payload["reconciled_at_epoch"] = now_epoch
                payload["updated_at_epoch"] = now_epoch
                payload["expires_at_epoch"] = now_epoch + RECENT_COMPLETION_TTL_SECONDS
                atomic_write_text(
                    path,
                    json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
                )
            else:
                path.unlink(missing_ok=True)
                return None
    elif state != "completed":
        path.unlink(missing_ok=True)
        return None
    return payload


def plan_pending(
    state_dir: Path,
    pending: list[PendingTask],
    *,
    now_epoch: float | None = None,
) -> QueuePlan:
    """Keep one task per queue identity and suppress recent cross-chat repeats."""
    now_value = time.time() if now_epoch is None else now_epoch
    seen: dict[str, dict[str, Any]] = {}
    candidates: list[PendingTask] = []
    suppressed: list[SuppressedTask] = []
    invalid: list[InvalidTask] = []

    for item in pending:
        _, task = item
        task_id = str(task.get("id", ""))
        try:
            key = queue_key(task)
        except InvalidDedupeMetadata as exc:
            invalid.append(InvalidTask(item=item, error=str(exc)))
            continue
        fingerprint = execution_fingerprint(task)
        _, revision = task_dedupe_identity(task)

        receipt = _read_receipt(state_dir, key, now_epoch=now_value)
        if receipt is not None and receipt["task_id"] != task_id:
            previous_revision = receipt.get("dedupe_revision", 1)
            if type(previous_revision) is not int:
                previous_revision = 1
            changed = receipt.get("execution_fingerprint") != fingerprint
            revised = revision > previous_revision
            if receipt["state"] == "completed" and revised:
                receipt = None
            elif changed or revised:
                invalid.append(InvalidTask(
                    item=item,
                    reason="dedupe_intent_conflict",
                    error=(
                        f"intent conflicts with task {receipt['task_id']!r} "
                        f"({receipt['state']}, revision {previous_revision}); "
                        "wait for completion, then publish a new task id with a higher dedupe_revision"
                    ),
                ))
                continue
        if receipt is not None and receipt["task_id"] != task_id:
            suppressed.append(
                SuppressedTask(
                    item=item,
                    duplicate_of=str(receipt["task_id"]),
                    queue_key=key,
                    execution_fingerprint=fingerprint,
                    reason="recent_duplicate",
                )
            )
            continue

        prior = seen.get(key)
        if prior is not None and prior["id"] != task_id:
            if (execution_fingerprint(prior) != fingerprint or
                    task_dedupe_identity(prior)[1] != revision):
                invalid.append(InvalidTask(
                    item=item,
                    reason="dedupe_intent_conflict",
                    error=f"intent already queued as task {prior['id']!r}; wait for its completion before revising",
                ))
                continue
            suppressed.append(
                SuppressedTask(
                    item=item,
                    duplicate_of=str(prior["id"]),
                    queue_key=key,
                    execution_fingerprint=fingerprint,
                    reason="queued_duplicate",
                )
            )
            continue

        seen[key] = task
        candidates.append(item)

    return QueuePlan(tuple(candidates), tuple(suppressed), tuple(invalid))


def _write_receipt(
    state_dir: Path,
    task: dict[str, Any],
    *,
    state: str,
    outcome: str | None,
    expires_at_epoch: float,
    now_epoch: float,
) -> None:
    key = queue_key(task)
    payload = {
        "version": 1,
        "queue_key": key,
        "task_id": str(task["id"]),
        "task_digest": task_digest(task),
        "execution_fingerprint": execution_fingerprint(task),
        "dedupe_revision": task_dedupe_identity(task)[1],
        "state": state,
        "outcome": outcome,
        "updated_at_epoch": now_epoch,
        "expires_at_epoch": expires_at_epoch,
    }
    path = _receipt_path(state_dir, key)
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(
        path,
        json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
    )


def record_admission(
    state_dir: Path,
    task: dict[str, Any],
    *,
    now_epoch: float | None = None,
) -> None:
    """Hold a queue identity while the task has a durable claim."""
    now_value = time.time() if now_epoch is None else now_epoch
    _write_receipt(
        state_dir,
        task,
        state="admitted",
        outcome=None,
        expires_at_epoch=now_value + task_timeout_for(task) + ADMISSION_GRACE_SECONDS,
        now_epoch=now_value,
    )


def record_completion(
    state_dir: Path,
    task: dict[str, Any],
    outcome: str,
    *,
    now_epoch: float | None = None,
) -> None:
    """Keep a short receipt so late duplicates from another chat are drained, not rerun."""
    now_value = time.time() if now_epoch is None else now_epoch
    _write_receipt(
        state_dir,
        task,
        state="completed",
        outcome=outcome,
        expires_at_epoch=now_value + RECENT_COMPLETION_TTL_SECONDS,
        now_epoch=now_value,
    )