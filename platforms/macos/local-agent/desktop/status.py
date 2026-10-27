"""Bounded local read-only desktop snapshot, with no Qt or service imports.

This provides presentation data only, not proof of operator authorization.
Status is a local observation, and process liveness cannot establish process
identity or execution authority. Files are never created or modified here.
"""
from __future__ import annotations

import json
import os
import re
import stat
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

MAX_STATUS_BYTES = 32 * 1024
MAX_REGISTRY_BYTES = 128 * 1024
MAX_REPOSITORIES = 128
DEFAULT_MAX_STATUS_AGE_SECONDS = 180
_ID = re.compile(r"^[A-Za-z0-9._-]{1,96}$")
_SHA = re.compile(r"^[0-9a-f]{40}$")
_VERSION = re.compile(r"^[A-Za-z0-9._+-]{1,48}$")


@dataclass(frozen=True, slots=True)
class DesktopSnapshot:
    """Schema-v1 values safe to render without exposing private paths or grants."""

    version: int
    observed_at: str
    state: str
    daemon_state: str | None
    daemon_version: str | None
    installed_source_sha: str | None
    status_updated_at: str | None
    status_age_seconds: int | None
    process_alive: bool | None
    current_task_id: str | None
    repository_count: int | None
    repository_ids: tuple[str, ...]
    registry_state: str
    disabled_marker_present: bool
    status_source: str
    registry_source: str
    warnings: tuple[str, ...]

    def as_record(self) -> dict[str, object]:
        """Return a UI/JSON-friendly copy of the sanitized snapshot."""
        return asdict(self)


def _read_private_json(path: Path, limit: int) -> dict[str, object]:
    """Reject special files, symlink leaves, foreign owners and unbounded JSON."""
    path = Path(path)
    if not path.is_absolute():
        raise ValueError("desktop observation path must be absolute")
    if stat.S_ISLNK(os.lstat(path).st_mode):
        raise ValueError("desktop observation source cannot be a symlink")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    flags |= getattr(os, "O_BINARY", 0)
    fd = os.open(path, flags)
    try:
        meta = os.fstat(fd)
        if not stat.S_ISREG(meta.st_mode):
            raise ValueError("desktop observation source is not a regular file")
        if os.name == "posix":
            if meta.st_uid != os.getuid() or meta.st_mode & 0o022:
                raise ValueError("desktop observation source is not private to the current user")
        if meta.st_size < 1 or meta.st_size > limit:
            raise ValueError("desktop observation source size is invalid")
        with os.fdopen(fd, "rb", closefd=False) as stream:
            raw = stream.read(limit + 1)
        if not raw or len(raw) > limit:
            raise ValueError("desktop observation source exceeds its byte limit")
    finally:
        os.close(fd)

    def unique_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate desktop observation key")
            result[key] = value
        return result

    record = json.loads(raw.decode("utf-8"), object_pairs_hook=unique_pairs)
    if not isinstance(record, dict):
        raise ValueError("desktop observation source must be a JSON object")
    return record


def _default_pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _owner_pid(record: dict[str, object]) -> int | None:
    for key in ("entrypoint_pid", "supervisor_pid", "pid"):
        value = record.get(key)
        if type(value) is int and value >= 2:
            return value
    return None


def _clean_string(value: object, regex: re.Pattern[str]) -> str | None:
    return value if isinstance(value, str) and regex.fullmatch(value) else None


def _timestamp(value: object) -> datetime | None:
    if not isinstance(value, str) or len(value) > 48:
        return None
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return result.astimezone(timezone.utc) if result.tzinfo is not None else None


def _registry_snapshot(path: Path) -> tuple[str, tuple[str, ...]]:
    try:
        record = _read_private_json(path, MAX_REGISTRY_BYTES)
        if type(record.get("version")) is not int or record["version"] != 1:
            raise ValueError("unsupported repository registry version")
        repos = record.get("repositories")
        if not isinstance(repos, list) or len(repos) > MAX_REPOSITORIES:
            raise ValueError("invalid repository registry count")
        seen: set[str] = set()
        result: list[str] = []
        for item in repos:
            if not isinstance(item, dict) or type(item.get("enabled", True)) is not bool:
                raise ValueError("invalid repository registry entry")
            name = _clean_string(item.get("id"), _ID)
            if name is None or name.casefold() in seen:
                raise ValueError("invalid or duplicate repository id")
            seen.add(name.casefold())
            if item.get("enabled", True):
                result.append(name)
        return "available", tuple(result)
    except (OSError, ValueError, UnicodeError, json.JSONDecodeError):
        return "unavailable", ()


def read_local_snapshot(
    *, status_file: Path, registry_file: Path, disabled_file: Path,
    now: datetime | None = None,
    pid_alive: Callable[[int], bool] | None = None,
    max_age_seconds: int = DEFAULT_MAX_STATUS_AGE_SECONDS,
) -> DesktopSnapshot:
    """Read local observations once. No network, subprocesses or daemon imports."""
    if type(max_age_seconds) is not int or not 1 <= max_age_seconds <= 3600:
        raise ValueError("max_age_seconds must be between 1 and 3600")
    moment = now or datetime.now(timezone.utc)
    if moment.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    moment = moment.astimezone(timezone.utc)
    present = False
    try:
        Path(disabled_file).lstat()
        present = True
    except FileNotFoundError:
        pass
    except OSError:
        present = True  # An unreadable marker must never look enabled.

    registry_state, repo_ids = _registry_snapshot(registry_file)
    warnings: list[str] = []
    if registry_state != "available":
        warnings.append("registry_unavailable")

    state = "unavailable"
    daemon_state: str | None = None
    version: str | None = None
    revision: str | None = None
    updated_at: str | None = None
    age: int | None = None
    alive: bool | None = None
    task_id: str | None = None
    try:
        record = _read_private_json(status_file, MAX_STATUS_BYTES)
        if record.get("version") != 1 or type(record.get("version")) is not int:
            raise ValueError("unsupported status schema")
        raw_state = record.get("state")
        if not isinstance(raw_state, str) or len(raw_state) > 48:
            raise ValueError("invalid daemon state")
        daemon_state = raw_state if raw_state in {
            "idle", "running", "disabled", "stopping", "stopped",
        } else None
        version = _clean_string(record.get("daemon_version"), _VERSION)
        revision = _clean_string(record.get("self_revision"), _SHA)
        task_id = _clean_string(record.get("current_task_id"), _ID)
        stamp = _timestamp(record.get("updated_at"))
        if stamp is None:
            warnings.append("status_timestamp_invalid")
            state = "stale"
        else:
            updated_at = stamp.isoformat()
            delta = (moment - stamp).total_seconds()
            age = max(0, int(delta))
            pid = _owner_pid(record)
            if pid is None:
                warnings.append("status_owner_unknown")
                state = "stale"
            else:
                try:
                    alive = bool((pid_alive or _default_pid_alive)(pid))
                except OSError:
                    alive = False
                if not alive:
                    warnings.append("status_owner_not_running")
                    state = "stale"
                elif delta < -30 or delta > max_age_seconds:
                    warnings.append("status_stale")
                    state = "stale"
                elif daemon_state is None:
                    warnings.append("status_state_unknown")
                    state = "unknown"
                elif daemon_state == "running":
                    state = "busy" if task_id is not None else "running"
                else:
                    state = daemon_state
    except (OSError, ValueError, UnicodeError, json.JSONDecodeError):
        warnings.append("status_unavailable")

    if present:
        state = "disabled"
    return DesktopSnapshot(
        version=1, observed_at=moment.isoformat(),
        state=state, daemon_state=daemon_state,
        daemon_version=version, installed_source_sha=revision,
        status_updated_at=updated_at, status_age_seconds=age,
        process_alive=alive, current_task_id=task_id,
        repository_count=len(repo_ids) if registry_state == "available" else None,
        repository_ids=repo_ids, registry_state=registry_state,
        disabled_marker_present=present,
        status_source="local_private_status_file",
        registry_source="local_private_registry_file",
        warnings=tuple(warnings),
    )


def read_default_local_snapshot(*, home: Path | None = None) -> DesktopSnapshot:
    """Read the existing macOS user-state layout; other OS paths are explicit.

    Windows/Linux discovery will be added only after native platform validation.
    This function never generates configuration or enrolls an installation.
    """
    base = (home or Path.home()) / "Library" / "Application Support" / "local-agent"
    return read_local_snapshot(
        status_file=base / "status.json",
        registry_file=base / "repositories.json",
        disabled_file=base / "disabled.json",
    )
