"""Small user-owned polling preference shared by CLI and existing supervisor.

No new scheduler or worker. An absent preference preserves existing
2/5/15-second project polling, with a faster 2/5/5-second host inbox.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import stat
import tempfile

from local_agent.supervisor import policy

PREFERENCE_FILE = "polling.json"
MIN_FIXED_SECONDS = 2
MAX_FIXED_SECONDS = 300
MAX_CONFIG_BYTES = 256


@dataclass(frozen=True)
class PollingSettings:
    mode: str = "adaptive"
    seconds: int = 15

    def as_dict(self) -> dict[str, object]:
        return {
            "mode": self.mode,
            "seconds": self.seconds,
            "adaptive_seconds": {"hot": 2, "warm": 5, "idle": 15},
        }


def parse_polling_settings(payload: object) -> PollingSettings:
    if (not isinstance(payload, dict) or set(payload) != {"version", "mode", "seconds"}
            or type(payload.get("version")) is not int or payload["version"] != 1):
        raise ValueError("polling preference requires version, mode and seconds")
    mode = payload["mode"]
    seconds = payload["seconds"]
    if mode not in ("fixed", "adaptive") or type(mode) is not str:
        raise ValueError("polling mode must be fixed or adaptive")
    if type(seconds) is not int or not MIN_FIXED_SECONDS <= seconds <= MAX_FIXED_SECONDS:
        raise ValueError("polling seconds must be 2..300")
    return PollingSettings(mode=mode, seconds=seconds)


def _require_private_state_dir(state_dir: Path) -> None:
    if not state_dir.is_absolute() or state_dir.is_symlink() or not state_dir.is_dir():
        raise ValueError("polling state directory must exist and be absolute")
    meta = state_dir.stat()
    if (not stat.S_ISDIR(meta.st_mode) or meta.st_uid != os.getuid()
            or meta.st_mode & 0o022):
        raise ValueError("polling state directory must belong to the user and not be writable by others")


def read_polling_settings(state_dir: Path) -> PollingSettings:
    _require_private_state_dir(state_dir)
    path = state_dir / PREFERENCE_FILE
    if path.is_symlink():
        raise ValueError("polling preference symlink is not permitted")
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except FileNotFoundError:
        return PollingSettings()
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_mode & 0o077 or not 0 < info.st_size <= MAX_CONFIG_BYTES):
            raise ValueError("polling preference file is not private or bounded")
        with os.fdopen(fd, "rb", closefd=False) as stream:
            raw = stream.read(MAX_CONFIG_BYTES + 1)
    finally:
        os.close(fd)
    if len(raw) > MAX_CONFIG_BYTES:
        raise ValueError("polling preference exceeds byte limit")
    try:
        def unique(items: list[tuple[str, object]]) -> dict[str, object]:
            value: dict[str, object] = {}
            for key, item in items:
                if key in value:
                    raise ValueError("duplicate polling preference field")
                value[key] = item
            return value
        return parse_polling_settings(json.loads(raw, object_pairs_hook=unique))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("invalid polling preference JSON") from exc


def save_polling_settings(state_dir: Path, settings: PollingSettings) -> None:
    _require_private_state_dir(state_dir)
    data = json.dumps(
        {"version": 1, "mode": settings.mode, "seconds": settings.seconds},
        sort_keys=True,
    ).encode("utf-8") + b"\n"
    parse_polling_settings(json.loads(data))
    path = state_dir / PREFERENCE_FILE
    if path.is_symlink():
        raise ValueError("polling preference symlink is not permitted")
    if path.exists():
        read_polling_settings(state_dir)  # Reject unexpected ownership or corruption.
    fd, name = tempfile.mkstemp(prefix=".polling-", dir=state_dir)
    staged = Path(name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(staged, path)
        directory_fd = os.open(state_dir, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        staged.unlink(missing_ok=True)


def repository_interval(settings: PollingSettings, last_activity_at: float | None, now: float) -> float:
    if settings.mode == "fixed":
        return float(settings.seconds)
    return policy.adaptive_poll_tier(last_activity_at, now)[1]


def control_interval(
    settings: PollingSettings, last_activity_at: float | None, now: float,
    *, host_intake_enabled: bool = False,
) -> float:
    if settings.mode == "fixed":
        return float(settings.seconds)
    interval = policy.adaptive_poll_tier(last_activity_at, now)[1]
    # The host inbox reads the local Git checkout; refreshing that checkout
    # only every 15 seconds would defeat its 5-second idle scan. Tighten
    # the existing control snapshot refresh only for enabled host intake.
    return min(interval, 5.0) if host_intake_enabled else interval


def host_interval(settings: PollingSettings, last_activity_at: float | None, now: float) -> float:
    if settings.mode == "fixed":
        return float(settings.seconds)
    # Host-only commands benefit from quick discovery even while other
    # project mailboxes remain idle at their established slower interval.
    return min(5.0, policy.adaptive_poll_tier(last_activity_at, now)[1])


class LivePollingPreferences:
    """One cached owner preference reader for the existing supervisor loop."""

    def __init__(self, state_dir: Path, log: object) -> None:
        self.state_dir = state_dir
        self.log = log
        self.settings = PollingSettings()
        self.last_activity_at: float | None = None
        self.last_read_at: float | None = None
        self.last_error_log_at: float | None = None

    def refresh(
        self, completed: dict[str, int],
        project_done: int, host_published: int, now: float,
    ) -> None:
        if any(
            code == project_done or (name.startswith("@host:") and code == host_published)
            for name, code in completed.items()
        ):
            self.last_activity_at = now
        if self.last_read_at is not None and now - self.last_read_at < 1.0:
            return
        self.last_read_at = now
        try:
            self.settings = read_polling_settings(self.state_dir)
            self.last_error_log_at = None
        except (OSError, ValueError) as exc:
            if self.last_error_log_at is None or now - self.last_error_log_at >= 60:
                self.log(
                    "polling preference ignored; preserving last valid mode: "
                    f"{type(exc).__name__}"
                )
                self.last_error_log_at = now

    def control(self, now: float, *, host_intake_enabled: bool = False) -> float:
        return control_interval(
            self.settings, self.last_activity_at, now,
            host_intake_enabled=host_intake_enabled,
        )

    def host(self, now: float) -> float:
        return host_interval(self.settings, self.last_activity_at, now)
