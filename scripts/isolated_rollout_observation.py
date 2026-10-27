"""Read-only source and queue observations for isolated rollout rehearsals.

Observations are never installation authority: no admission freeze, restart or
trusted operator grant is implied by a green snapshot. No daemon imports.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
from typing import Any

from scripts.isolated_rollout_controller import BASE_SHA, RolloutRejected

_NAME = re.compile(r"^[A-Za-z0-9._-]{1,120}\.json$")
_MAX_FILE = 65536
_MAX_ENTRIES = 10000


def _pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in items:
        if key in out:
            raise RolloutRejected("duplicate field in observed JSON")
        out[key] = value
    return out


def _read_json(path: Path) -> dict[str, Any]:
    if path.is_symlink():
        raise RolloutRejected("observed JSON symlink rejected")
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= _MAX_FILE:
            raise RolloutRejected("observed JSON is not a bounded regular file")
        with os.fdopen(fd, "rb", closefd=False) as handle:
            raw = handle.read(_MAX_FILE + 1)
    finally:
        os.close(fd)
    if len(raw) > _MAX_FILE:
        raise RolloutRejected("observed JSON exceeded read bound")
    try:
        result = json.loads(raw.decode("utf-8"), object_pairs_hook=_pairs)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise RolloutRejected("invalid observed JSON") from exc
    if not isinstance(result, dict):
        raise RolloutRejected("observed JSON root is not an object")
    return result


def _names(folder: Path) -> frozenset[str]:
    if folder.is_symlink() or not folder.is_dir():
        raise RolloutRejected("missing or unsafe queue directory")
    items = frozenset()
    with os.scandir(folder) as iterator:
        collected: set[str] = set()
        for index, entry in enumerate(iterator):
            if index >= _MAX_ENTRIES:
                raise RolloutRejected("queue directory exceeds entry bound")
            if entry.name == ".gitkeep" and entry.is_file(follow_symlinks=False):
                continue
            if not _NAME.fullmatch(entry.name) or not entry.is_file(follow_symlinks=False):
                raise RolloutRejected("invalid or nonregular queue entry")
            collected.add(entry.name)
        items = frozenset(collected)
    return items


def _git(checkout: Path, *args: str) -> str:
    env = os.environ.copy()
    env["GIT_OPTIONAL_LOCKS"] = "0"
    process = subprocess.run(
        ["git", "-C", str(checkout), *args],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        timeout=15, check=False, env=env,
    )
    if process.returncode:
        raise RolloutRejected("Git provenance observation failed")
    if len(process.stdout) > _MAX_FILE:
        raise RolloutRejected("Git provenance output too large")
    return process.stdout.strip()


def source_proof(checkout: Path) -> dict[str, Any]:
    """Verify exact installed revision and an unmodified source checkout."""
    if checkout.is_symlink() or not checkout.is_dir():
        raise RolloutRejected("invalid source directory")
    actual = checkout.resolve(strict=True)
    revision = _git(actual, "rev-parse", "--verify", "HEAD")
    if not re.fullmatch(r"[a-f0-9]{40}", revision):
        raise RolloutRejected("invalid source revision")
    if revision != BASE_SHA:
        raise RolloutRejected("source SHA is not the accepted installed base")
    if _git(actual, "status", "--porcelain", "--untracked-files=normal"):
        raise RolloutRejected("source checkout is not clean")
    # Bind paths to an audit fingerprint; a future installation transaction
    # still needs a separate exclusive lock and freshly checked source.
    return {"source_sha": revision, "source_path": str(actual)}


def observe_local_status(status_file: Path, *, max_age_seconds: int = 120) -> dict[str, Any]:
    if type(max_age_seconds) is not int or not 1 <= max_age_seconds <= 600:
        raise RolloutRejected("invalid status freshness threshold")
    data = _read_json(status_file)
    if data.get("version") != 1 or data.get("self_revision") != BASE_SHA:
        raise RolloutRejected("status source or schema differs from installed base")
    if data.get("state") != "idle":
        raise RolloutRejected("daemon is not reported idle")
    if data.get("current_task_id") is not None or data.get("current_attempt_id") is not None:
        raise RolloutRejected("daemon is reporting a task in flight")
    if data.get("active_repository_ids") or data.get("active_host_task_ids"):
        raise RolloutRejected("supervisor has active workers")
    raw = data.get("updated_at")
    if not isinstance(raw, str):
        raise RolloutRejected("status timestamp missing")
    try:
        stamp = dt.datetime.fromisoformat(raw)
    except ValueError as exc:
        raise RolloutRejected("malformed status timestamp") from exc
    if stamp.tzinfo is None:
        raise RolloutRejected("status timestamp missing timezone")
    age = (dt.datetime.now(dt.timezone.utc) - stamp.astimezone(dt.timezone.utc)).total_seconds()
    if age < -10 or age > max_age_seconds:
        raise RolloutRejected("status observation stale or in the future")
    return {"state": "idle", "status_age_seconds": round(age, 3),
            "supervisor_pid": data.get("supervisor_pid"), "source_sha": BASE_SHA}


def observe_queue(
    *,
    task_dir: Path,
    result_dir: Path,
    claim_dir: Path,
    spool_dir: Path,
    host_claim_dir: Path | None = None,
    host_result_dir: Path | None = None,
) -> dict[str, Any]:
    """Only count persisted names; never read task commands, credentials or grants."""
    tasks, results = _names(task_dir), _names(result_dir)
    claims, spool = _names(claim_dir), _names(spool_dir)
    pending = tasks - results
    if host_claim_dir is None and host_result_dir is not None:
        raise RolloutRejected("incomplete host ledger paths")
    if host_claim_dir is not None and host_result_dir is None:
        raise RolloutRejected("incomplete host ledger paths")
    # Terminal UNKNOWN and interrupted/ambiguous outcomes remain manual
    # reconciliation gates, even when their result files are present.
    if len(results) > 256:
        raise RolloutRejected("too many results for bounded UNKNOWN inspection")
    unresolved = 0
    for name in sorted(results):
        result = _read_json(result_dir / name)
        status = result.get("status")
        if status not in {"done", "failed", "cancelled", "canceled"}:
            unresolved += 1
        if result.get("failure_reason") in {
            "interrupted_previous_attempt", "interrupted_execution",
            "ambiguous_publication", "ambiguous_push",
        }:
            unresolved += 1
    host_unknown = frozenset()
    host_unresolved = 0
    if host_claim_dir is not None:
        host_results = _names(host_result_dir)
        if len(host_results) > 256:
            raise RolloutRejected("too many host results for bounded UNKNOWN inspection")
        host_unknown = _names(host_claim_dir) - host_results
        for name in sorted(host_results):
            entry = _read_json(host_result_dir / name)
            if entry.get("status") not in {"done", "failed", "cancelled", "canceled"}:
                host_unresolved += 1
            if entry.get("failure_reason") in {
                "interrupted_execution", "interrupted_previous_attempt",
                "ambiguous_publication", "ambiguous_push",
            }:
                host_unresolved += 1
    return {
        "tasks_total": len(tasks),
        "results_total": len(results),
        "pending_tasks": len(pending),
        "active_or_stale_claims": len(claims),
        "publication_spool": len(spool),
        "host_unreconciled_claims": len(host_unknown),
        "unreconciled_results": unresolved + host_unresolved,
        "quiescent_observation": not any(
            (pending, claims, spool, host_unknown, unresolved, host_unresolved)
        ),
    }


def observe_twice(
    *,
    source: Path,
    status: Path,
    mailboxes: tuple[tuple[Path, Path], ...],
    claim_dir: Path,
    spool_dir: Path,
    expected_mailboxes: int,
    host_claim_dir: Path | None = None,
    host_result_dir: Path | None = None,
) -> dict[str, Any]:
    """Conservative read-only evidence, explicitly *not* switch authorization."""
    if type(expected_mailboxes) is not int or expected_mailboxes < 1:
        raise RolloutRejected("expected project mailbox count missing")
    if len(mailboxes) != expected_mailboxes:
        raise RolloutRejected("project mailbox inventory is incomplete")
    if len(set((str(t.resolve(strict=False)), str(r.resolve(strict=False)))
               for t, r in mailboxes)) != expected_mailboxes:
        raise RolloutRejected("duplicate mailbox path")
    def snapshot() -> tuple[dict[str, Any], dict[str, Any], tuple[dict[str, Any], ...]]:
        proof = source_proof(source)
        service = observe_local_status(status)
        queues = tuple(observe_queue(task_dir=t, result_dir=r, claim_dir=claim_dir,
                                      spool_dir=spool_dir, host_claim_dir=host_claim_dir,
                                      host_result_dir=host_result_dir)
                       for t, r in mailboxes)
        return proof, service, queues
    first = snapshot()
    second = snapshot()
    stable = (first[0] == second[0] and first[2] == second[2]
              and first[1]["supervisor_pid"] == second[1]["supervisor_pid"])
    settled = stable and all(item["quiescent_observation"] for item in second[2])
    return {
        "version": 1,
        "source": second[0],
        "status": second[1],
        "project_mailboxes": expected_mailboxes,
        "queues": list(second[2]),
        "double_read_stable": stable,
        "quiescent_observation": settled,
        "switch_authorized": False,
        "requires_exclusive_admission_fence": True,
        "evidence_digest": hashlib.sha256(
            json.dumps([first, second], sort_keys=True).encode("utf-8")
        ).hexdigest(),
    }
