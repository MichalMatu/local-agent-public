"""Explicit on-demand Debian Agent service/spool diagnostic; no queue control."""
from __future__ import annotations

import os
import re
import stat
import subprocess

from linux_agent.host_ops_artifacts import _parent

_FILE = re.compile(r"^[A-Za-z0-9._-]{1,200}\.json$")
_MAX_ENTRIES = 20_000
_UNIT = "local-agent-linux.service"


class StatusInspectionError(ValueError):
    """State could not be inspected without ambiguity."""


def _names(root_fd: int, directory: str) -> set[str]:
    fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=root_fd)
    try:
        found: set[str] = set()
        with os.scandir(fd) as iterator:
            for index, entry in enumerate(iterator):
                if index >= _MAX_ENTRIES:
                    raise StatusInspectionError("too many local ledger entries to inspect")
                if not _FILE.fullmatch(entry.name):
                    continue
                if not entry.is_file(follow_symlinks=False):
                    raise StatusInspectionError("unsafe claim/result entry")
                found.add(entry.name[:-5])
        return found
    finally:
        os.close(fd)


def _stop(root_fd: int) -> bool:
    try:
        found = os.stat("EMERGENCY_STOP", dir_fd=root_fd, follow_symlinks=False)
    except FileNotFoundError:
        return False
    if not stat.S_ISREG(found.st_mode):
        raise StatusInspectionError("unsafe emergency STOP entry")
    return True


def _service() -> dict:
    """Fixed read-only user-systemd query, never stop/start/restart."""
    try:
        p = subprocess.run(
            ["/usr/bin/systemctl", "--user", "show", "-p", "ActiveState",
             "-p", "UnitFileState", "-p", "MainPID", _UNIT],
            stdin=subprocess.DEVNULL, capture_output=True, timeout=4, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return {"available": False, "active_state": None, "unit_file_state": None,
                "main_pid": None}
    if p.returncode != 0 or len(p.stdout) > 2048 or len(p.stderr) > 2048:
        return {"available": False, "active_state": None, "unit_file_state": None,
                "main_pid": None}
    values = dict(line.split("=", 1) for line in p.stdout.decode("utf-8", errors="replace").splitlines()
                  if "=" in line)
    pid = values.get("MainPID")
    return {
        "available": "ActiveState" in values and "UnitFileState" in values,
        "active_state": values.get("ActiveState"),
        "unit_file_state": values.get("UnitFileState"),
        "main_pid": int(pid) if pid and pid.isdecimal() else None,
    }


def inspect_status(state_path: str) -> dict:
    try:
        with _parent(state_path) as (parent_fd, name):
            fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                         dir_fd=parent_fd)
            try:
                info = os.fstat(fd)
                if info.st_uid != os.getuid() or info.st_mode & 0o077:
                    raise StatusInspectionError("state root must be owner-private")
                claims = _names(fd, "claims")
                results = _names(fd, "results")
                stop = _stop(fd)
            finally:
                os.close(fd)
    except OSError as exc:
        raise StatusInspectionError("could not inspect explicit state root") from exc
    return {
        "state_path": state_path,
        "emergency_stop_present": stop,
        "local_claim_count": len(claims),
        "local_spooled_result_count": len(results),
        "claims_without_local_spool": len(claims - results),
        "claim_result_counts_are_not_live_queue_size": True,
        "git_pending_tasks": None,
        "worker_locks_busy": None,
        "service_unit": _UNIT,
        "service": _service(),
    }
