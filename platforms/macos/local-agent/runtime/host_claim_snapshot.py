"""Read-only, bounded host-task claim census for status and CLI diagnostics.

Local claim files are durable historical records, not active-worker evidence.
Never inspect task payloads or acquire/recover task/lease locks here.
"""
from __future__ import annotations

import os
import re
import stat
from pathlib import Path

MAX_RECORDS = 4096
_RECORD = re.compile(r"^[A-Za-z0-9._-]{1,120}\.json$")
_KINDS = ("requests", "claims", "results", "remote-approvals")


def _check_directory(path: Path, *, private: bool = True) -> None:
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode):
        raise ValueError("host_task_directory_unsafe")
    forbidden = 0o077 if private else 0o022
    if os.name == "posix" and (info.st_uid != os.getuid() or info.st_mode & forbidden):
        raise ValueError("host_task_directory_untrusted")


def _record_names(path: Path) -> set[str]:
    try:
        _check_directory(path)
    except FileNotFoundError:
        return set()  # Host task directories are created lazily.
    names: set[str] = set()
    with os.scandir(path) as entries:
        for entry in entries:
            if (
                len(names) >= MAX_RECORDS
                or _RECORD.fullmatch(entry.name) is None
                or not entry.is_file(follow_symlinks=False)
            ):
                raise ValueError("host_task_record_unsafe_or_unbounded")
            info = entry.stat(follow_symlinks=False)
            if not stat.S_ISREG(info.st_mode) or (
                os.name == "posix"
                and (info.st_uid != os.getuid() or info.st_mode & 0o077)
            ):
                raise ValueError("host_task_record_untrusted")
            names.add(entry.name)
    return names


def observe_host_claims(*, state_root: Path) -> dict[str, object]:
    """Report empty/nonempty/unknown claims without treating old tasks as live.

    This snapshot intentionally cannot prove held advisory locks, workers,
    remote Git inbox freshness, or safe restart. No filesystem mutations.
    """
    result: dict[str, object] = {
        "schema_version": 1,
        "status": "unverified",
        "claims_state": "unknown",
        "claim_files": None,
        "unresolved_claims": None,
        "requests_without_result": None,
        "result_files": None,
        "remote_approval_files": None,
        "active_workers_verified": False,
        "active_leases_verified": False,
        "restart_safe": None,
        "warnings": [],
        "mutated": False,
    }
    root = Path(state_root)
    if not root.is_absolute():
        result["warnings"] = ["invalid_state_root"]
        return result
    try:
        _check_directory(root, private=False)
        base = root / "host-tasks"
        try:
            _check_directory(base)
        except FileNotFoundError:
            records = {name: set() for name in _KINDS}
        else:
            records = {name: _record_names(base / name) for name in _KINDS}
    except (OSError, ValueError) as exc:
        result["warnings"] = [str(exc) if isinstance(exc, ValueError) else "host_task_ledger_unavailable"]
        return result
    requests, claims, results, approvals = (records[name] for name in _KINDS)
    result.update({
        "status": "observed",
        "claims_state": "empty" if not claims else "nonempty",
        "claim_files": len(claims),
        "unresolved_claims": len(claims - results),
        "requests_without_result": len(requests - results),
        "result_files": len(results),
        "remote_approval_files": len(approvals),
    })
    return result


def host_status_fields(state_root: Path) -> dict[str, object]:
    """Explicitly distinguish local claim census from unobserved workers."""
    return {
        "host_claims": observe_host_claims(state_root=state_root),
        "active_workers_observed": False,
        "active_worker_count": None,
    }
