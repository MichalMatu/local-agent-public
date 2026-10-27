from __future__ import annotations

from local_agent.repository.context import RepositoryContext

POLL_SECONDS = 15.0
HOT_POLL_SECONDS = 2.0
WARM_POLL_SECONDS = 5.0
HOT_WINDOW_SECONDS = 30.0
WARM_WINDOW_SECONDS = 120.0
SUPERVISOR_CONTROL_POLL_SECONDS = POLL_SECONDS
WORKER_TURN_GRACE_SECONDS = 3600


def adaptive_poll_tier(
    last_activity_at: float | None,
    now: float,
) -> tuple[str, float]:
    "Return the polling tier for one repository from its latest task activity."
    if last_activity_at is None:
        return "idle", POLL_SECONDS
    age = max(0.0, now - last_activity_at)
    if age < HOT_WINDOW_SECONDS:
        return "hot", HOT_POLL_SECONDS
    if age < WARM_WINDOW_SECONDS:
        return "warm", WARM_POLL_SECONDS
    return "idle", POLL_SECONDS


def interval_due(last_at: float | None, interval: float, now: float) -> bool:
    return last_at is None or now - last_at >= interval


def interval_remaining(last_at: float | None, interval: float, now: float) -> float:
    if last_at is None:
        return 0.0
    return max(0.0, interval - max(0.0, now - last_at))


def ordered_repositories(
    repositories: list[RepositoryContext],
    start_after: str | None,
) -> list[RepositoryContext]:
    if not repositories or start_after is None:
        return list(repositories)
    for index, repository in enumerate(repositories):
        if repository.repository_id == start_after:
            start = (index + 1) % len(repositories)
            return repositories[start:] + repositories[:start]
    return list(repositories)
