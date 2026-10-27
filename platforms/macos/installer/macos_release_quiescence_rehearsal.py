"""Conservative read-only ledger inventory under a sandbox-only Mac freeze.

This deliberately cannot inspect an installed Mac state root or authorize
switching source. It never modifies, resumes or replays a task.
"""
from __future__ import annotations

import datetime as dt
import fcntl
import json
import os
from pathlib import Path
import re
import stat
import sys
from typing import Any

from local_agent.supervisor import macos_release_admission as gate
from local_agent.repository.context import (
    repository_from_dict, repository_config_digest, validate_repository_set,
)
from scripts import isolated_rollout_controller as rollout
from scripts.macos_release_fence_rehearsal import _FROZEN_DATA, _private_state

_NAME = re.compile(r"^[A-Za-z0-9._-]{1,120}\.json$")
_ID = re.compile(r"^[A-Za-z0-9._-]{1,100}$")
_LIMIT = 256
_MAX_RESULT_BYTES = 32 * 1024
_UNSAFE_REASONS = frozenset({
    "interrupted_previous_attempt", "interrupted_execution",
    "ambiguous_publication", "ambiguous_push",
})
_TERMINAL = frozenset({"done", "failed", "cancelled", "canceled"})


def _read_private_regular(path: Path, *, max_bytes: int) -> bytes:
    if path.is_symlink():
        raise rollout.RolloutRejected("symlinked release evidence rejected")
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except OSError as exc:
        raise rollout.RolloutRejected("missing private release evidence") from exc
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= max_bytes
                or info.st_uid != os.getuid() or info.st_mode & 0o077
                or info.st_nlink != 1):
            raise rollout.RolloutRejected("release evidence ownership/mode/size invalid")
        with os.fdopen(fd, "rb", closefd=False) as handle:
            raw = handle.read(max_bytes + 1)
    finally:
        os.close(fd)
    if len(raw) > max_bytes:
        raise rollout.RolloutRejected("release evidence exceeded byte limit")
    return raw


def _pairs_unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for name, item in pairs:
        if name in value:
            raise rollout.RolloutRejected("duplicate release evidence JSON key")
        value[name] = item
    return value


def _json_record(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(
            _read_private_regular(path, max_bytes=_MAX_RESULT_BYTES).decode("utf-8"),
            object_pairs_hook=_pairs_unique,
        )
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise rollout.RolloutRejected("invalid release evidence JSON") from exc
    if not isinstance(value, dict):
        raise rollout.RolloutRejected("release evidence JSON must be an object")
    return value


def _dir(path: Path) -> Path:
    if path.is_symlink():
        raise rollout.RolloutRejected("symlinked release evidence directory")
    try:
        info = path.lstat()
    except OSError as exc:
        raise rollout.RolloutRejected("missing release evidence directory") from exc
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
            or info.st_mode & 0o077):
        raise rollout.RolloutRejected("release evidence directory not private")
    return path


def _names(directory: Path) -> frozenset[str]:
    _dir(directory)
    found: set[str] = set()
    with os.scandir(directory) as entries:
        for item in entries:
            if len(found) >= _LIMIT:
                raise rollout.RolloutRejected("release ledger exceeds entry bound")
            if _NAME.fullmatch(item.name) is None:
                raise rollout.RolloutRejected("unexpected release ledger entry")
            if item.is_symlink() or not item.is_file(follow_symlinks=False):
                raise rollout.RolloutRejected("unsafe release ledger file")
            found.add(item.name)
    return frozenset(found)


def _result_is_terminal(path: Path) -> bool:
    record = _json_record(path)
    return (record.get("status") in _TERMINAL
            and record.get("failure_reason") not in _UNSAFE_REASONS
            and record.get("replayed") is not True)


def _ledger(request_dir: Path, result_dir: Path, claim_dir: Path | None = None,
            spool_dir: Path | None = None) -> dict[str, int | bool]:
    requests = _names(request_dir)
    results = _names(result_dir)
    claims = _names(claim_dir) if claim_dir is not None else frozenset()
    spools = _names(spool_dir) if spool_dir is not None else frozenset()
    nonterminal = sum(not _result_is_terminal(result_dir / item) for item in results)
    pending = requests - results
    return {
        "requests": len(requests), "results": len(results),
        "pending": len(pending), "claims": len(claims), "spool": len(spools),
        "nonterminal": nonterminal,
        "settled": not (pending or claims or spools or nonterminal),
    }


def _supervisor(state: Path, max_age_seconds: int) -> dict[str, Any]:
    record = _json_record(state / "status.json")
    if (record.get("state") != "idle"
            or record.get("self_revision") != rollout.BASE_SHA
            or type(record.get("pid")) is not int or record["pid"] < 1):
        raise rollout.RolloutRejected("supervisor source/idle/PID evidence mismatch")
    try:
        moment = dt.datetime.fromisoformat(record["updated_at"])
        if moment.tzinfo is None:
            raise ValueError("naive status")
        age = (dt.datetime.now(dt.timezone.utc)
               - moment.astimezone(dt.timezone.utc)).total_seconds()
    except (ValueError, TypeError, KeyError) as exc:
        raise rollout.RolloutRejected("malformed supervisor timestamp") from exc
    if age < -5 or age > max_age_seconds:
        raise rollout.RolloutRejected("supervisor state not fresh")
    return {"observed_state": "idle", "self_revision": rollout.BASE_SHA,
            "supervisor_pid": record["pid"], "age_seconds": round(age, 3)}




def _private_within(root: Path, path: Path) -> Path:
    """Reject symlinked ancestors and any registry route outside the sandbox."""
    if not path.is_absolute() or root == path or root not in path.parents:
        raise rollout.RolloutRejected("registry workspace escapes Mac sandbox")
    current = root
    for segment in path.relative_to(root).parts:
        current = current / segment
        _dir(current)
    if path.resolve(strict=True) != path:
        raise rollout.RolloutRejected("noncanonical Mac registry checkout")
    return path


def _native_registry_mailboxes(
    root: Path, original_root: Path, state: Path, expected_ids: tuple[str, ...],
) -> dict[str, tuple[Path, Path, Path, Path, dict[str, Any]]]:
    """Resolve the real RepositoryContext routing schema against sandbox paths.

    No legacy fallback, disabled entries, unlisted host namespaces or
    implicit empty registry are admissible for an installation rehearsal.
    """
    raw = _read_private_regular(state / "repositories.json", max_bytes=128 * 1024)
    try:
        payload = json.loads(raw.decode("utf-8"), object_pairs_hook=_pairs_unique)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise rollout.RolloutRejected("invalid native Mac registry JSON") from exc
    if (not isinstance(payload, dict) or set(payload) != {"version", "repositories"}
            or type(payload["version"]) is not int or payload["version"] != 1
            or not isinstance(payload["repositories"], list)
            or len(payload["repositories"]) != len(expected_ids)):
        raise rollout.RolloutRejected("incomplete native Mac registry")
    entries = payload["repositories"]
    if any(not isinstance(item, dict) or item.get("enabled", True) is not True
           for item in entries):
        raise rollout.RolloutRejected("disabled/invalid native Mac registry entry")
    # RepositoryContext normalizes workspace paths with Path.resolve().
    # Inspect their raw spellings first: otherwise a symlink in the source
    # registry disappears, potentially redirecting an evidence inspection.
    for entry in entries:
        for field in ("control_dir", "work_dir", "checkpoints_dir", "host_identity_file"):
            value = entry.get(field)
            if value is None:
                continue
            if not isinstance(value, str):
                raise rollout.RolloutRejected("invalid native Mac registry path field")
            unexpanded = root / value[2:] if value.startswith("~/") else Path(value)
            # The system temp root can have an OS-maintained alias:
            # /var/folders/... -> /private/var/folders/... on macOS.
            # Rebase ONLY the full supplied sandbox-root prefix, never
            # resolve a candidate path (which would hide hostile symlinks).
            if unexpanded == original_root or original_root in unexpanded.parents:
                unexpanded = root / unexpanded.relative_to(original_root)
            if field == "host_identity_file":
                _private_within(root, unexpanded.parent)
                _read_private_regular(unexpanded, max_bytes=32 * 1024)
            else:
                _private_within(root, unexpanded)
    try:
        repositories = validate_repository_set(
            [repository_from_dict(item, home=root) for item in entries],
        )
    except ValueError as exc:
        raise rollout.RolloutRejected("invalid native Mac repository routing") from exc
    if {item.repository_id for item in repositories} != set(expected_ids):
        raise rollout.RolloutRejected("native Mac registry project inventory mismatch")
    state_root = _dir(state / "repositories")
    if {item.name for item in state_root.iterdir()} != set(expected_ids):
        raise rollout.RolloutRejected("native Mac private repository state mismatch")

    result: dict[str, tuple[Path, Path, Path, Path, dict[str, Any]]] = {}
    for item in repositories:
        # Every workspace needs to be a real, private and non-redirected
        # directory. Only sandbox test roots are permitted here.
        for path in (item.control, item.work, item.checkpoints):
            _private_within(root, path)
        control = _private_within(root, item.control / ".agent")
        root_state = _dir(state_root / item.repository_id)
        if item.host_mailbox_enabled:
            assert item.host_id is not None
            mailbox = _private_within(root, control / "hosts" / item.host_id)
            host_states = _dir(root_state / "hosts")
            if {entry.name for entry in host_states.iterdir()} != {item.host_id}:
                raise rollout.RolloutRejected("native Mac host state routing mismatch")
            repo_state = _dir(host_states / item.host_id)
        else:
            if (root_state / "hosts").exists() or (root_state / "hosts").is_symlink():
                raise rollout.RolloutRejected("unexpected host namespace for Mac project")
            mailbox = control
            repo_state = root_state
        tasks = _dir(mailbox / "tasks")
        results = _dir(mailbox / "results")
        claims = _dir(repo_state / "claims")
        spool = _dir(repo_state / "result-spool")
        result[item.repository_id] = (
            tasks, results, claims, spool,
            {"repository": item.repository, "control_branch": item.control_branch,
             "host_mailbox": item.host_mailbox_enabled,
             "host_id": item.host_id,
             "config_digest": repository_config_digest(item)},
        )
    return result

def inspect_disposable_mac_quiescence(root: Path, *, project_ids: tuple[str, ...],
                                      max_age_seconds: int = 120,
                                      layout_mode: str = "synthetic") -> dict[str, Any]:
    """Capture only exact, closed ledgers after simulated exclusive freeze.

    The inventory is deliberately conservative: any pending work, claim,
    spool, or unresolved result blocks an offline acceptance. No installed
    state can be supplied because _sandbox and _private_state enforce paths.
    """
    if sys.platform != "darwin":
        raise rollout.RolloutRejected("macOS-only sandbox quiescence rehearsal")
    if (not isinstance(project_ids, tuple) or not project_ids
            or len(project_ids) > 32 or len(set(project_ids)) != len(project_ids)
            or any(type(item) is not str or _ID.fullmatch(item) is None
                   for item in project_ids)):
        raise rollout.RolloutRejected("incomplete or invalid project inventory")
    if type(max_age_seconds) is not int or not 1 <= max_age_seconds <= 300:
        raise rollout.RolloutRejected("invalid heartbeat freshness bound")
    if layout_mode not in {"synthetic", "native_registry"}:
        raise rollout.RolloutRejected("invalid Mac queue layout mode")
    original_root = Path(root).absolute()
    root = rollout._sandbox(root)
    state = _private_state(root)
    lock_path = state / gate.LOCK
    if lock_path.is_symlink():
        raise rollout.RolloutRejected("sandbox release lock symlink")
    try:
        fd = os.open(lock_path, os.O_RDWR | getattr(os, "O_NOFOLLOW", 0))
    except OSError as exc:
        raise rollout.RolloutRejected("sandbox release lock missing") from exc
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_mode & 0o077 or info.st_nlink != 1):
            raise rollout.RolloutRejected("sandbox release lock not private")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (OSError, BlockingIOError) as exc:
            raise rollout.RolloutRejected("cannot inspect while a Mac task is active") from exc
        try:
            marker = _read_private_regular(state / gate.FROZEN, max_bytes=1024)
            if marker != _FROZEN_DATA:
                raise rollout.RolloutRejected("invalid durable Mac freeze marker")
            # Opt-in is required, not merely presence of a frozen marker.
            if not gate._active_config(state):
                raise rollout.RolloutRejected("Mac release admission not opted in")
            supervisor = _supervisor(state, max_age_seconds)
            registry = _dir(state / "repositories")
            observed = {p.name for p in registry.iterdir()}
            if observed != set(project_ids):
                raise rollout.RolloutRejected("Mac project inventory mismatch")
            projects: dict[str, dict[str, int | bool]] = {}
            routing: dict[str, dict[str, Any]] = {}
            if layout_mode == "native_registry":
                layout = _native_registry_mailboxes(root, original_root, state, project_ids)
                for project, (tasks, results, claims, spool, metadata) in layout.items():
                    projects[project] = _ledger(tasks, results, claims, spool)
                    routing[project] = metadata
            else:
                mailboxes = _dir(root / "mailboxes")
                if {p.name for p in mailboxes.iterdir()} != set(project_ids):
                    raise rollout.RolloutRejected("Mac task mailbox inventory mismatch")
                for project in project_ids:
                    repo = _dir(registry / project)
                    mailbox = _dir(mailboxes / project)
                    projects[project] = _ledger(
                        mailbox / "tasks", mailbox / "results",
                        repo / "claims", repo / "result-spool",
                    )
            host_root = _dir(state / "host-tasks")
            host = _ledger(
                host_root / "requests", host_root / "results", host_root / "claims",
            )
            approvals = _names(host_root / "remote-approvals")
            if approvals - _names(host_root / "results"):
                raise rollout.RolloutRejected("unsettled Mac remote host approval")
            convo = _dir(state / "conversation-operator")
            conversation = _ledger(convo / "requests", convo / "results")
            launches = _names(convo / "launches")
            if launches:
                # A consumed-once browser campaign can require manual UNKNOWN
                # reconciliation even when a result is present.
                raise rollout.RolloutRejected("unreconciled Conversation launch marker")
            settled = (all(item["settled"] for item in projects.values())
                       and host["settled"] and conversation["settled"])
            return {
                "version": 1, "sandbox_only": True,
                "project_count": len(projects),
                "layout_mode": layout_mode, "registry_routing": routing,
                "supervisor": supervisor, "projects": projects,
                "host": host, "conversation": conversation,
                "quiescent_observation": settled,
                "source_switch_authorized": False,
                "real_queue_exclusion_proven": False,
                "tasks_replayed": False,
            }
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)
