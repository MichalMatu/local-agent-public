"""Shared read-only operational overview for CLI and future frontends.

Compose existing bounded local observers; never infer execution/replay rights
from status files, claim counts or Git transport. This is an advisory view,
not an atomic multi-file snapshot or an admission gate.
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Callable

from local_agent.runtime.backlog_snapshot import observe_local_backlog
from local_agent.runtime.health_snapshot import observe_local_health

_PENDING_KINDS = ("claims", "result-spool", "corrupt-claims")


def observe_local_overview(
    *,
    status_file: Path,
    registry_file: Path,
    state_root: Path,
    max_age_seconds: int = 180,
    now: datetime | None = None,
    pid_alive: Callable[[int], bool] | None = None,
) -> dict[str, object]:
    """Combine two independent local observations into one bounded contract.

    'attention' only means there are durable records to inspect; those
    records may be active work, and are NEVER instructions to retry/clean.
    A missing or unsafe input stays unverified rather than becoming zero.
    """
    health = observe_local_health(
        status_file=status_file, registry_file=registry_file,
        max_age_seconds=max_age_seconds, now=now, pid_alive=pid_alive,
    )
    backlog = observe_local_backlog(
        state_root=state_root, registry_file=registry_file,
    )
    totals = backlog["totals"]
    assert isinstance(totals, dict)
    record_counts = {kind: totals[kind] for kind in _PENDING_KINDS}
    records_present = (
        None if any(value is None for value in record_counts.values())
        else any(value > 0 for value in record_counts.values())
    )

    if health["status"] == "unverified" or backlog["status"] == "unverified":
        overall = "unverified"
    elif health["status"] == "degraded":
        overall = "degraded"
    elif records_present:
        overall = "attention"
    else:
        overall = "healthy"

    warnings = sorted({
        *(f"health:{warning}" for warning in health["warnings"]),
        *(f"backlog:{warning}" for warning in backlog["warnings"]),
    })
    return {
        "schema_version": 1,
        "status": overall,
        "health": health,
        "backlog": backlog,
        "summary": {
            "state": health["state"],
            "repository_count": health["repository_count"],
            "current_task_id": health["current_task_id"],
            "active_workers_observed": health["active_workers_observed"],
            "active_worker_count": health["active_worker_count"],
            "max_parallel_workers_observed": health["max_parallel_workers_observed"],
            "record_counts": record_counts,
            "records_present": records_present,
        },
        "warnings": warnings,
        "source": "local_files",
        "consistent_point_in_time": False,
        "execution_authority": "not_asserted",
        "replay_authority": "not_asserted",
        "mutated": False,
    }
