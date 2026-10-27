"""Bounded, local-only daemon observation for CLI and future frontends.

This is presentation evidence, not a trusted operator control or task gate.
Do not expose raw files, repository paths, credentials or task content.
"""
from __future__ import annotations

import json
import os
import re
import stat
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

STATUS_BYTES = 32 * 1024
REGISTRY_BYTES = 128 * 1024
MAX_REPOSITORIES = 128
_ID = re.compile(r"^[A-Za-z0-9._-]{1,96}$")
_SHA = re.compile(r"^[0-9a-f]{40}$")
_VERSION = re.compile(r"^[A-Za-z0-9._+-]{1,48}$")
_STATES = frozenset({"idle", "running", "disabled", "stopping", "stopped"})


def _private_json(path: Path, limit: int) -> dict[str, object]:
    """Refuse links, special files, oversized JSON and unsafe ownership."""
    path = Path(path)
    if not path.is_absolute():
        raise ValueError("observation path must be absolute")
    if path.is_symlink():
        raise ValueError("observation symlink refused")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    fd = os.open(path, flags)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= limit:
            raise ValueError("invalid observation source")
        if os.name == "posix" and (info.st_uid != os.getuid() or info.st_mode & 0o022):
            raise ValueError("unsafe observation ownership")
        raw = os.read(fd, limit + 1)
        if not 0 < len(raw) <= limit:
            raise ValueError("invalid observation size")
    finally:
        os.close(fd)

    def unique(pairs: list[tuple[str, object]]) -> dict[str, object]:
        obj: dict[str, object] = {}
        for key, value in pairs:
            if key in obj:
                raise ValueError("duplicate observation key")
            obj[key] = value
        return obj

    result = json.loads(raw.decode("utf-8"), object_pairs_hook=unique)
    if not isinstance(result, dict):
        raise ValueError("observation must be an object")
    return result


def _safe_string(raw: object, regex: re.Pattern[str]) -> str | None:
    return raw if isinstance(raw, str) and regex.fullmatch(raw) else None


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _owner_pid(status: dict[str, object]) -> int | None:
    for key in ("entrypoint_pid", "supervisor_pid", "pid"):
        value = status.get(key)
        if type(value) is int and value >= 2:
            return value
    return None


def _repository_ids(registry: dict[str, object]) -> list[str]:
    if type(registry.get("version")) is not int or registry["version"] != 1:
        raise ValueError("unsupported registry version")
    entries = registry.get("repositories")
    if not isinstance(entries, list) or len(entries) > MAX_REPOSITORIES:
        raise ValueError("invalid registry")
    seen: set[str] = set()
    result: list[str] = []
    for item in entries:
        if not isinstance(item, dict):
            raise ValueError("invalid registry entry")
        enabled = item.get("enabled", True)
        name = _safe_string(item.get("id"), _ID)
        if type(enabled) is not bool or name is None or name.casefold() in seen:
            raise ValueError("invalid registry identity")
        seen.add(name.casefold())
        if enabled:
            result.append(name)
    return result


def observe_local_health(
    *,
    status_file: Path,
    registry_file: Path,
    now: datetime | None = None,
    pid_alive: Callable[[int], bool] | None = None,
    max_age_seconds: int = 180,
) -> dict[str, object]:
    """Return a stable, sanitized snapshot without performing any effects."""
    if type(max_age_seconds) is not int or not 1 <= max_age_seconds <= 3600:
        raise ValueError("max_age_seconds must be between 1 and 3600")
    moment = now or datetime.now(timezone.utc)
    if moment.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    moment = moment.astimezone(timezone.utc)
    warnings: list[str] = []
    daemon_state = "unavailable"
    version = None
    revision = None
    task_id = None
    age = None
    owner_alive: bool | None = None
    state = "unverified"
    repositories: list[str] | None = None
    workers_observed = False
    worker_count: int | None = None
    worker_limit: int | None = None

    try:
        status = _private_json(status_file, STATUS_BYTES)
        if type(status.get("version")) is not int or status["version"] != 1:
            raise ValueError("unsupported status version")
        raw_state = status.get("state")
        if raw_state not in _STATES or not isinstance(raw_state, str):
            warnings.append("status_state_invalid")
        else:
            daemon_state = raw_state
        version = _safe_string(status.get("daemon_version"), _VERSION)
        revision = _safe_string(status.get("self_revision"), _SHA)
        task_id = _safe_string(status.get("current_task_id"), _ID)
        stamp = status.get("updated_at")
        if not isinstance(stamp, str) or len(stamp) > 48:
            warnings.append("status_timestamp_invalid")
        else:
            try:
                timestamp = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
                if timestamp.tzinfo is None:
                    raise ValueError("timestamp must have timezone")
                seconds = (moment - timestamp.astimezone(timezone.utc)).total_seconds()
                age = max(0, int(seconds))
                if seconds < -30 or seconds > max_age_seconds:
                    warnings.append("status_stale")
            except ValueError:
                warnings.append("status_timestamp_invalid")
        pid = _owner_pid(status)
        if pid is None:
            warnings.append("status_owner_unknown")
        else:
            try:
                owner_alive = bool((pid_alive or _alive)(pid))
            except OSError:
                owner_alive = False
            if not owner_alive:
                warnings.append("status_owner_not_running")
        # Only a supervisor-issued, explicitly measured count is advisory evidence.
        # Durable claims and generic daemon status are never worker-count evidence.
        raw_observed = status.get("active_workers_observed")
        if raw_observed is True:
            count = status.get("active_worker_count")
            limit = status.get("max_parallel_workers")
            supervisor_pid = status.get("supervisor_pid")
            if (
                status.get("execution_model") == "parallel_repository_supervisor"
                and type(supervisor_pid) is int and supervisor_pid >= 2
                and type(count) is int and type(limit) is int
                and 1 <= limit <= 64 and 0 <= count <= limit
            ):
                try:
                    supervisor_alive = bool((pid_alive or _alive)(supervisor_pid))
                except OSError:
                    supervisor_alive = False
                if supervisor_alive:
                    workers_observed = True
                    worker_count = count
                    worker_limit = limit
                else:
                    warnings.append("worker_observer_not_running")
            else:
                warnings.append("worker_sample_invalid")
        elif raw_observed is not None and raw_observed is not False:
            warnings.append("worker_sample_invalid")
        if not warnings:
            state = "busy" if daemon_state == "running" and task_id else daemon_state
    except (OSError, ValueError, UnicodeError, json.JSONDecodeError, TypeError):
        warnings.append("status_unavailable")

    try:
        repositories = _repository_ids(_private_json(registry_file, REGISTRY_BYTES))
    except (OSError, ValueError, UnicodeError, json.JSONDecodeError, TypeError):
        warnings.append("registry_unavailable")

    healthy = not warnings and state in ("idle", "running", "busy")
    if warnings:
        state = "unverified"
        workers_observed = False
        worker_count = None
        worker_limit = None
    return {
        "schema_version": 1,
        "status": "healthy" if healthy else ("unverified" if warnings else "degraded"),
        "state": state,
        "observed_daemon_state": daemon_state,
        "daemon_version": version,
        "source_revision": revision,
        "status_age_seconds": age,
        "process_alive": owner_alive,
        "current_task_id": task_id,
        "repository_count": len(repositories) if repositories is not None else None,
        "repository_ids": repositories,
        "active_workers_observed": workers_observed,
        "active_worker_count": worker_count,
        "max_parallel_workers_observed": worker_limit,
        "warnings": warnings,
        "source": "local_files",
        "execution_authority": "not_asserted",
    }
