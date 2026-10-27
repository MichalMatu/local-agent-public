"""Read-only aggregate telemetry for the Local Agent operator surface."""

from __future__ import annotations

import json
import os
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import local_agent.daemon.service as agentd
from local_agent.conversation import operator_contract, operator_queue
from local_agent.repository.context import RepositoryContext
from local_agent.supervisor import conversation as conversation_supervisor

REMOTE_OPERATOR_STATUS = ".agent/status/operator.json"
OPERATOR_STATUS_SCHEMA_VERSION = 1
OPERATOR_STATUS_HEARTBEAT_SECONDS = 5 * 60
OPERATOR_STATUS_PUBLISH_TIMEOUT_SECONDS = 5
MAX_DEDUPE_EVIDENCE_FILES_PER_REPOSITORY = 128

_TRUTHY = frozenset({"1", "true", "yes", "on"})


def _read_json_object(path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None
    return payload if isinstance(payload, dict) else None


def _bounded_json_sample(path: Path) -> tuple[list[Path], bool]:
    """Read at most one bounded directory sample; never walk lifetime history."""
    if not path.is_dir():
        return [], False
    ranked: list[tuple[float, str, Path]] = []
    try:
        with os.scandir(path) as entries:
            for visited, entry in enumerate(entries):
                if visited > MAX_DEDUPE_EVIDENCE_FILES_PER_REPOSITORY:
                    break
                if not entry.name.endswith(".json"):
                    continue
                try:
                    if not entry.is_file(follow_symlinks=False):
                        continue
                    ranked.append((
                        entry.stat(follow_symlinks=False).st_mtime,
                        entry.name,
                        Path(entry.path),
                    ))
                except OSError:
                    continue
    except OSError:
        return [], False
    truncated = len(ranked) > MAX_DEDUPE_EVIDENCE_FILES_PER_REPOSITORY
    ranked.sort(key=lambda value: (value[0], value[1]), reverse=True)
    return [
        item[2]
        for item in ranked[:MAX_DEDUPE_EVIDENCE_FILES_PER_REPOSITORY]
    ], truncated


def _reason_counts(counter: Counter[str]) -> dict[str, int]:
    return {key: counter[key] for key in sorted(counter)}


def dedupe_observability(repositories: Iterable[RepositoryContext]) -> dict[str, Any]:
    suppressions: Counter[str] = Counter()
    rejections: Counter[str] = Counter()
    reconciliations: Counter[str] = Counter()
    run_records = 0
    receipt_records = 0
    truncated = False
    now_epoch = time.time()

    for repository in repositories:
        state_dir = agentd.STATE_DIR / "repositories" / repository.repository_id

        paths, limited = _bounded_json_sample(state_dir / "runs")
        truncated = truncated or limited
        for path in paths:
            payload = _read_json_object(path)
            if payload is None:
                continue
            run_records += 1
            event = str(payload.get("event") or "")
            if event == "duplicate_task_suppressed":
                suppressions[str(payload.get("duplicate_reason") or "unknown")] += 1
            elif event == "task_rejected":
                reason = str(payload.get("failure_reason") or "")
                if reason in {"dedupe_intent_conflict", "invalid_dedupe_key"}:
                    rejections[reason] += 1

        paths, limited = _bounded_json_sample(state_dir / "task-dedupe")
        truncated = truncated or limited
        for path in paths:
            payload = _read_json_object(path)
            if payload is None:
                continue
            expires_at = payload.get("expires_at_epoch")
            if not isinstance(expires_at, (int, float)) or isinstance(expires_at, bool):
                continue
            if float(expires_at) <= now_epoch:
                continue
            receipt_records += 1
            if payload.get("reconciled_from_published_run") is True:
                reason = str(
                    payload.get("reconciliation_reason")
                    or "published_run_after_claim_release"
                )
                reconciliations[reason] += 1

    return {
        "evidence_scope": "bounded_directory_sample",
        "suppressed_count": sum(suppressions.values()),
        "suppression_reasons": _reason_counts(suppressions),
        "rejected_count": sum(rejections.values()),
        "rejection_reasons": _reason_counts(rejections),
        "reconciled_count": sum(reconciliations.values()),
        "reconciliation_reasons": _reason_counts(reconciliations),
        "observed_run_records": run_records,
        "observed_receipt_records": receipt_records,
        "scan_truncated": truncated,
    }


def _operator_configuration() -> tuple[bool, bool, str | None]:
    source = os.environ
    enabled = source.get(
        conversation_supervisor.OPERATOR_ENABLED_ENV, ""
    ).strip().lower() in _TRUTHY
    required = (
        conversation_supervisor.OPERATOR_ROOT_ENV,
        conversation_supervisor.OPERATOR_CHECKOUT_ENV,
        conversation_supervisor.OPERATOR_PRODUCTION_CHECKOUT_ENV,
    )
    if any(not source.get(name, "").strip() for name in required):
        return enabled, False, "missing required operator paths" if enabled else None

    probe = dict(source)
    probe[conversation_supervisor.OPERATOR_ENABLED_ENV] = "1"
    try:
        configured = conversation_supervisor.runtime_config(probe) is not None
    except ValueError as exc:
        return enabled, False, str(exc)
    return enabled, configured, None if configured else "operator configuration unavailable"


def _project_operator_request(
    campaign: conversation_supervisor.RunningOperatorCampaign | None,
) -> tuple[dict[str, Any] | None, str | None]:
    if campaign is None:
        return None, None
    try:
        request = operator_contract.load_operator_request(campaign.request_path)
        operator_contract.operator_request_repository_ids(request)
    except OSError:
        return None, "operator_request_io_error"
    except ValueError:
        return None, "operator_request_invalid"
    return {
        "workflow_id": str(request["workflow_id"]),
        "children_total": len(request["children"]),
    }, None


def operator_observability(
    campaign: conversation_supervisor.RunningOperatorCampaign | None,
) -> dict[str, Any]:
    enabled, configured, configuration_error = _operator_configuration()
    running = bool(campaign is not None and campaign.proc.poll() is None)
    request, request_error = _project_operator_request(campaign if running else None)
    try:
        result_publish_pending = conversation_supervisor.result_publish_pending()
    except (OSError, ValueError):
        result_publish_pending = False
        if configuration_error is None:
            configuration_error = "operator_result_state_unavailable"
    reconciliation_required = False
    if enabled and configured and not running:
        try:
            staged = operator_queue.next_staged_request(agentd.STATE_DIR)
            reconciliation_required = (
                staged is not None
                and operator_queue.launch_reconciliation_required(agentd.STATE_DIR, staged)
            )
        except (OSError, ValueError, RuntimeError):
            reconciliation_required = True
            if configuration_error is None:
                configuration_error = "operator_launch_state_unavailable"
    return {
        "enabled": enabled,
        "configured": configured,
        "running": running,
        "launch_reconciliation_required": reconciliation_required,
        "configuration_error": configuration_error,
        "active_request": request,
        "active_request_error": request_error,
        "result_publish_pending": result_publish_pending,
    }


def build_operator_status(
    repositories: Iterable[RepositoryContext],
    campaign: conversation_supervisor.RunningOperatorCampaign | None,
    *,
    max_workers: int,
) -> dict[str, Any]:
    repository_list = list(repositories)
    return {
        "schema_version": OPERATOR_STATUS_SCHEMA_VERSION,
        "daemon": {
            "daemon_version": agentd.DAEMON_VERSION,
            "self_revision": agentd.self_revision(),
            "execution_model": "parallel_repository_supervisor",
            "max_parallel_workers": max_workers,
            "repository_count": len(repository_list),
        },
        "operator": operator_observability(campaign),
        "dedupe": dedupe_observability(repository_list),
    }


def _without_updated_at(payload: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in payload.items() if key != "updated_at"}


def _heartbeat_due(existing: dict[str, Any] | None) -> bool:
    raw = existing.get("updated_at") if isinstance(existing, dict) else None
    if not isinstance(raw, str) or not raw:
        return True
    try:
        updated = datetime.fromisoformat(raw)
        if updated.tzinfo is None:
            updated = updated.replace(tzinfo=timezone.utc)
    except ValueError:
        return True
    age = (datetime.now(timezone.utc) - updated.astimezone(timezone.utc)).total_seconds()
    return age < 0 or age >= OPERATOR_STATUS_HEARTBEAT_SECONDS


def publish_operator_status(
    repositories: Iterable[RepositoryContext],
    campaign: conversation_supervisor.RunningOperatorCampaign | None,
    *,
    max_workers: int,
    force: bool = False,
) -> bool:
    """Publish read-only aggregate status only on semantic change or heartbeat."""
    payload = build_operator_status(repositories, campaign, max_workers=max_workers)
    path = agentd.core.CONTROL / REMOTE_OPERATOR_STATUS
    existing = _read_json_object(path)
    unchanged = existing is not None and _without_updated_at(existing) == payload
    if not force and unchanged and not _heartbeat_due(existing):
        return False

    payload["updated_at"] = agentd.now_iso()
    agentd.publish_control_json(
        REMOTE_OPERATOR_STATUS,
        payload,
        commit_message="Agent operator status",
        timeout=OPERATOR_STATUS_PUBLISH_TIMEOUT_SECONDS,
        attempts=1,
    )
    return True
