"""Pure scheduling policy helpers for the bounded-parallel supervisor."""

from __future__ import annotations

import os
from dataclasses import dataclass
from enum import Enum

from local_agent.supervisor import policy as supervisor_policy
from local_agent.supervisor.polling import PollingSettings, repository_interval

MAX_WORKERS_ENV = "LOCAL_AGENT_MAX_PARALLEL_WORKERS"
DEFAULT_MAX_WORKERS = 1
MAX_MAX_WORKERS = 4
RESOURCE_RETRY_BACKOFF_SECONDS = (2.0, 5.0, 10.0, 30.0, 60.0)
WORKER_FAILURE_RETRY_BASE_SECONDS = 2.0
WORKER_FAILURE_RETRY_MAX_SECONDS = 300.0
CONTROL_DEFER_RETRY_BASE_SECONDS = 2.0
CONTROL_DEFER_RETRY_MAX_SECONDS = 15.0
CONTROL_LEASE_BUSY_DRAIN_ATTEMPTS = 6
REPEATED_FAILURE_LOG_SECONDS = 60.0
OPERATOR_IDLE_HEARTBEAT_SECONDS = 300.0
LOCAL_LOG_MAINTENANCE_SECONDS = 30.0


class ControlLeaseBusyAction(Enum):
    RETRY = "retry"
    PAUSE_CONTROL_REPOSITORY = "pause_control_repository"
    DRAIN_ALL = "drain_all"


@dataclass
class RepositorySchedule:
    last_poll_at: float | None = None
    last_activity_at: float | None = None
    retry_not_before: float = 0.0
    consecutive_failures: int = 0
    last_failure_code: int | None = None
    last_failure_log_at: float | None = None
    resource_deferrals: int = 0
    last_deferral_log_at: float | None = None


@dataclass
class ControlDeferralState:
    """Retry and admission state for one supervisor control-repository identity."""

    repository_id: str | None = None
    retry_not_before: float = 0.0
    consecutive_deferrals: int = 0
    consecutive_lease_busy: int = 0
    last_log_at: float | None = None
    paused_repository_id: str | None = None


def format_operator_idle_summary(repository_count: int, max_workers: int) -> str:
    noun = "repository" if repository_count == 1 else "repositories"
    return f"IDLE no active task ({repository_count} {noun}); max_workers={max_workers}"


def operator_idle_log_due(last_idle_log_at: float | None, now: float) -> bool:
    return last_idle_log_at is None or now - last_idle_log_at >= OPERATOR_IDLE_HEARTBEAT_SECONDS


def local_log_maintenance_due(last_maintenance_at: float | None, now: float) -> bool:
    return (
        last_maintenance_at is None
        or now - last_maintenance_at >= LOCAL_LOG_MAINTENANCE_SECONDS
    )


def bounded_retry_seconds(attempt: int, *, base: float, maximum: float) -> float:
    if attempt <= 0:
        return 0.0
    return min(maximum, base * (2 ** min(attempt - 1, 16)))


def resource_retry_seconds(attempt: int) -> float:
    if attempt <= 0:
        return 0.0
    index = min(attempt - 1, len(RESOURCE_RETRY_BACKOFF_SECONDS) - 1)
    return RESOURCE_RETRY_BACKOFF_SECONDS[index]


def worker_failure_retry_seconds(attempt: int) -> float:
    return bounded_retry_seconds(
        attempt,
        base=WORKER_FAILURE_RETRY_BASE_SECONDS,
        maximum=WORKER_FAILURE_RETRY_MAX_SECONDS,
    )


def control_defer_retry_seconds(attempt: int) -> float:
    return bounded_retry_seconds(
        attempt,
        base=CONTROL_DEFER_RETRY_BASE_SECONDS,
        maximum=CONTROL_DEFER_RETRY_MAX_SECONDS,
    )


def reset_control_deferral_state(
    state: ControlDeferralState,
    *,
    clear_pause: bool = False,
) -> None:
    state.retry_not_before = 0.0
    state.consecutive_deferrals = 0
    state.consecutive_lease_busy = 0
    state.last_log_at = None
    if clear_pause:
        state.paused_repository_id = None


def bind_control_repository(state: ControlDeferralState, repository_id: str) -> bool:
    """Reset retry evidence when the configured global-control identity changes."""
    if state.repository_id == repository_id:
        return False
    state.repository_id = repository_id
    reset_control_deferral_state(state, clear_pause=True)
    return True


def record_control_deferral(
    state: ControlDeferralState,
    *,
    now: float,
    lease_busy: bool,
) -> float:
    """Record one deferred probe while keeping lease-busy streak semantics exact."""
    state.consecutive_deferrals += 1
    if lease_busy:
        state.consecutive_lease_busy += 1
    else:
        state.consecutive_lease_busy = 0
    retry = control_defer_retry_seconds(state.consecutive_deferrals)
    state.retry_not_before = now + retry
    return retry


def control_lease_busy_should_force_drain(attempt: int) -> bool:
    return attempt >= CONTROL_LEASE_BUSY_DRAIN_ATTEMPTS


def control_lease_busy_action(
    state: ControlDeferralState,
    *,
    control_repository_running: bool,
) -> ControlLeaseBusyAction:
    """Classify repeated lease contention without performing scheduler side effects."""
    if not control_lease_busy_should_force_drain(state.consecutive_lease_busy):
        return ControlLeaseBusyAction.RETRY
    if control_repository_running:
        return ControlLeaseBusyAction.PAUSE_CONTROL_REPOSITORY
    return ControlLeaseBusyAction.DRAIN_ALL


def repeated_failure_log_due(last_log_at: float | None, now: float) -> bool:
    return last_log_at is None or now - last_log_at >= REPEATED_FAILURE_LOG_SECONDS


def reset_worker_failure_state(schedule: RepositorySchedule) -> None:
    schedule.consecutive_failures = 0
    schedule.last_failure_code = None
    schedule.last_failure_log_at = None


def reset_resource_deferral_state(schedule: RepositorySchedule) -> None:
    schedule.resource_deferrals = 0
    schedule.last_deferral_log_at = None


def resolve_max_workers(cli_value: int | None) -> int:
    raw: object = cli_value if cli_value is not None else os.environ.get(
        MAX_WORKERS_ENV,
        str(DEFAULT_MAX_WORKERS),
    )
    try:
        value = int(raw)
    except (TypeError, ValueError):
        raise ValueError(f"invalid {MAX_WORKERS_ENV}: {raw!r}") from None
    if value < 1 or value > MAX_MAX_WORKERS:
        raise ValueError(f"{MAX_WORKERS_ENV} must be 1..{MAX_MAX_WORKERS}, got {value}")
    return value


def repository_due(
    schedule: RepositorySchedule, now: float, settings: PollingSettings | None = None,
) -> bool:
    if schedule.retry_not_before > 0.0:
        return now >= schedule.retry_not_before
    interval = repository_interval(settings or PollingSettings(), schedule.last_activity_at, now)
    return supervisor_policy.interval_due(schedule.last_poll_at, interval, now)


def next_repository_delay(
    schedules: dict[str, RepositorySchedule],
    repository_ids: list[str],
    now: float,
    settings: PollingSettings | None = None,
) -> float:
    """Return the next due delay without depending on repository object shape."""
    if not repository_ids:
        return supervisor_policy.POLL_SECONDS
    delays: list[float] = []
    for repository_id in repository_ids:
        schedule = schedules.setdefault(repository_id, RepositorySchedule())
        if schedule.retry_not_before > 0.0:
            delays.append(max(0.0, schedule.retry_not_before - now))
            continue
        interval = repository_interval(settings or PollingSettings(), schedule.last_activity_at, now)
        delays.append(supervisor_policy.interval_remaining(schedule.last_poll_at, interval, now))
    return min(delays)
