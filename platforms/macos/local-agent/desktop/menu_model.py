"""Qt-independent, bounded presentation model for the optional desktop tray."""
from __future__ import annotations

from dataclasses import dataclass

from local_agent.desktop.status import DesktopSnapshot

MAX_MENU_REPOSITORIES = 10
_LABELS = {
    "idle": "Idle",
    "running": "Running",
    "busy": "Busy",
    "disabled": "Disabled",
    "stale": "Stale status",
    "unknown": "Unknown state",
    "unavailable": "Unavailable",
    "stopping": "Stopping",
    "stopped": "Stopped",
}
_WARNINGS = {
    "registry_unavailable": "Repository information unavailable",
    "status_timestamp_invalid": "Status timestamp invalid",
    "status_owner_unknown": "Status process unknown",
    "status_owner_not_running": "Status process not running",
    "status_stale": "Status heartbeat is stale",
    "status_state_unknown": "Status state not recognized",
    "status_unavailable": "Local status file unavailable",
}


@dataclass(frozen=True, slots=True)
class TrayPresentation:
    """Everything the UI may render; never raw JSON or private filesystem paths."""

    title: str
    state_label: str
    tooltip: str
    information: tuple[str, ...]
    repositories: tuple[str, ...]
    warnings: tuple[str, ...]


def build_tray_presentation(snapshot: DesktopSnapshot) -> TrayPresentation:
    state = _LABELS.get(snapshot.state, "Unknown state")
    information = [f"Agent: {state}"]
    if snapshot.daemon_version:
        information.append(f"Daemon version: {snapshot.daemon_version}")
    if snapshot.installed_source_sha:
        information.append(f"Reported source: {snapshot.installed_source_sha[:12]}")
    if snapshot.status_age_seconds is not None:
        information.append(f"Local status age: {snapshot.status_age_seconds}s")
    else:
        information.append("Local status age: unavailable")
    if snapshot.current_task_id and snapshot.state == "busy":
        information.append(f"Active task: {snapshot.current_task_id}")
    if snapshot.repository_count is None:
        information.append("Repositories: unavailable")
    else:
        information.append(f"Repositories: {snapshot.repository_count}")
    information.append("Source: local files (not remote authorization)")
    repos = list(snapshot.repository_ids[:MAX_MENU_REPOSITORIES])
    hidden = len(snapshot.repository_ids) - len(repos)
    if hidden:
        repos.append(f"+{hidden} more")
    if snapshot.repository_count == 0:
        repos = ["No managed repositories"]
    if snapshot.repository_count is None:
        repos = ["Repository information unavailable"]
    warnings = tuple(_WARNINGS[code] for code in snapshot.warnings if code in _WARNINGS)
    return TrayPresentation(
        title="Local Agent",
        state_label=state,
        tooltip=f"Local Agent — {state}",
        information=tuple(information),
        repositories=tuple(repos),
        warnings=warnings,
    )


def unavailable_presentation() -> TrayPresentation:
    return TrayPresentation(
        title="Local Agent",
        state_label="Unavailable",
        tooltip="Local Agent — unavailable",
        information=("Agent: Unavailable", "Source: local files (not remote authorization)"),
        repositories=("Repository information unavailable",),
        warnings=("Local status cannot be read",),
    )
