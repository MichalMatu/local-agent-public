"""Read-only macOS LaunchAgent provenance evidence for future rollout design.

No launchctl state mutation, daemon imports or source-switch authority. A live
admission fence and independent operator approval are separate requirements.
"""
from __future__ import annotations

import os
from pathlib import Path
import plistlib
import re
import stat
import subprocess
from typing import Callable

from scripts.isolated_rollout_controller import RolloutRejected

_LABEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_VALUE = re.compile(r"^\s*(state|pid)\s*=\s*([A-Za-z0-9._-]+)\s*$")
_MAX_OUTPUT = 256 * 1024
_MAX_PLIST = 64 * 1024


def _installed_plist(path: Path) -> dict:
    if path.is_symlink():
        raise RolloutRejected("LaunchAgent plist must not be a symlink")
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= _MAX_PLIST:
            raise RolloutRejected("invalid or oversized LaunchAgent plist")
        with os.fdopen(fd, "rb", closefd=False) as stream:
            raw = stream.read(_MAX_PLIST + 1)
    finally:
        os.close(fd)
    if len(raw) > _MAX_PLIST:
        raise RolloutRejected("oversized LaunchAgent plist")
    try:
        data = plistlib.loads(raw)
    except (ValueError, TypeError, KeyError, OverflowError) as exc:
        raise RolloutRejected("malformed LaunchAgent plist") from exc
    if not isinstance(data, dict):
        raise RolloutRejected("LaunchAgent plist must be a dictionary")
    return data


def inspect_launchagent(
    *, plist_path: Path, label: str, expected_source: Path,
    uid: int | None = None,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    verify_pid: bool = True,
) -> dict:
    """Observe launchd process and installed source without any service change."""
    if not isinstance(label, str) or not _LABEL.fullmatch(label):
        raise RolloutRejected("unsafe LaunchAgent label")
    if (not expected_source.is_dir() or expected_source.is_symlink()
            or not expected_source.is_absolute()):
        raise RolloutRejected("expected Mac source must be an absolute real directory")
    source = expected_source.resolve(strict=True)
    info = _installed_plist(plist_path)
    if info.get("Label") != label or info.get("Disabled") is True:
        raise RolloutRejected("LaunchAgent label or enabled state mismatch")
    arguments = info.get("ProgramArguments")
    if (not isinstance(arguments, list) or not arguments
            or len(arguments) > 64
            or any(not isinstance(a, str) or not a or "\x00" in a for a in arguments)):
        raise RolloutRejected("LaunchAgent ProgramArguments are malformed")
    # Explicitly resolve absolute arguments; substring matches are insufficient.
    source_arguments = []
    for argument in arguments:
        candidate = Path(argument)
        if candidate.is_absolute():
            canonical = candidate.resolve(strict=False)
            if canonical == source or source in canonical.parents:
                source_arguments.append(argument)
    if not source_arguments:
        raise RolloutRejected("LaunchAgent source not anchored in ProgramArguments")
    if info.get("RunAtLoad") is not True:
        raise RolloutRejected("unexpected LaunchAgent RunAtLoad policy")
    owner = os.getuid() if uid is None else uid
    if type(owner) is not int or not 0 <= owner < 2**31:
        raise RolloutRejected("invalid launchd UID")
    try:
        result = runner(
            ["launchctl", "print", f"gui/{owner}/{label}"],
            capture_output=True, text=True, timeout=15, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RolloutRejected("read-only launchd observation failed") from exc
    if result.returncode or not isinstance(result.stdout, str) or len(result.stdout) > _MAX_OUTPUT:
        raise RolloutRejected("launchd unavailable or status exceeds read bound")
    # The top-level service state and PID are the first occurrence in the
    # launchctl print output. Additional nested service properties can exist.
    found: dict[str, str] = {}
    for line in result.stdout.splitlines():
        match = _VALUE.fullmatch(line)
        if match:
            found.setdefault(match.group(1), match.group(2))
            if len(found) == 2:
                break
    if found.get("state") != "running":
        raise RolloutRejected("LaunchAgent is not running")
    try:
        pid = int(found.get("pid", "0"))
    except ValueError as exc:
        raise RolloutRejected("invalid LaunchAgent PID") from exc
    if not 1 <= pid < 2**31:
        raise RolloutRejected("missing active LaunchAgent PID")
    if verify_pid:
        try:
            os.kill(pid, 0)
        except (ProcessLookupError, PermissionError) as exc:
            raise RolloutRejected("LaunchAgent PID does not pass local liveness probe") from exc
    return {
        "manager": "launchd",
        "label": label,
        "pid": pid,
        "source_path": str(source),
        "source_arguments": len(source_arguments),
        "service_running": True,
        "admission_fenced": False,
        "source_switch_authorized": False,
    }
