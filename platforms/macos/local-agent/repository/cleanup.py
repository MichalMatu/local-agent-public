#!/usr/bin/env python3
"""Bounded cleanup for Git-backed Local Agent runtime metadata."""
from __future__ import annotations

import errno
import json
import os
import re
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any

from local_agent.foundation import storage
from local_agent.foundation.process import RESOURCE_LEASE_FDS_ENV, termination_critical_section
from local_agent.repository.history_policy import (
    CONTROL_HISTORY_COMPACT_ROOT_MESSAGE,
    CONTROL_HISTORY_POLICY_TRAILER,
)

TERMINAL_PAIR_RETENTION = 32
RUN_RETENTION = 32
ACK_RETENTION = 16
ORPHAN_RESULT_RETENTION = 8
CONTROL_HISTORY_COMPACTION_THRESHOLD = max(1, storage.CONTROL_HISTORY_DEPTH // 2)
_RUNTIME_PREFIXES = (
    ".agent/tasks/",
    ".agent/results/",
    ".agent/runs/",
    ".agent/daemon/acks/",
)
_TIMESTAMP_FIELDS = (
    "updated_at",
    "finished_at",
    "completed_at",
    "ended_at",
    "started_at",
    "persisted_at",
)
_TASK_ID_RE = re.compile(r"^[A-Za-z0-9._-]+$")


def _read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _timestamp(payload: dict[str, Any]) -> float:
    for field in _TIMESTAMP_FIELDS:
        raw = payload.get(field)
        if not isinstance(raw, str) or not raw:
            continue
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            continue
        try:
            return parsed.timestamp()
        except (OverflowError, OSError, ValueError):
            continue
    return 0.0


def _sort_key(path: Path) -> tuple[float, str]:
    return (_timestamp(_read_json(path)), path.name)


def _json_files(directory: Path) -> list[Path]:
    if not directory.exists():
        return []
    return sorted(path for path in directory.glob("*.json") if path.is_file())


def _task_payload_files(tasks_dir: Path, task_id: str) -> list[Path] | None:
    """Select only source-recognized payload files; None means protect the pair.

    Existing unreferenced data and symlinks are never safe to delete merely
    because a terminal task/result record exists in the Git control branch.
    """
    if _TASK_ID_RE.fullmatch(task_id) is None:
        return []
    payload_dir = tasks_dir / f"{task_id}.payload"
    if payload_dir.is_symlink():
        return None
    if not payload_dir.exists():
        return []
    if not payload_dir.is_dir():
        return None
    allowed_subdirs = {
        "writes": ".txt",
        "commands": ".sh",
        "verify-commands": ".sh",
        "steps": ".sh",
        "verify-steps": ".sh",
    }
    files: list[Path] = []
    entries = 0
    for path in payload_dir.rglob("*"):
        entries += 1
        if entries > 2048 or path.is_symlink():
            return None
        parts = path.relative_to(payload_dir).parts
        if path.is_dir():
            if len(parts) != 1 or parts[0] not in allowed_subdirs:
                return None
            continue
        if not path.is_file():
            return None
        if parts != ("patch.diff",):
            if len(parts) != 2 or parts[0] not in allowed_subdirs:
                return None
            extension = allowed_subdirs[parts[0]]
            basename = parts[1]
            if (not basename.endswith(extension)
                    or len(basename[:-len(extension)]) != 3
                    or not basename[:-len(extension)].isdigit()
                    or not 1 <= int(basename[:-len(extension)]) <= 256):
                return None
        files.append(path)
    return sorted(files)


def _relative(control: Path, path: Path) -> str:
    return path.relative_to(control).as_posix()


def _selected_empty_payload_directories(paths: tuple[str, ...]) -> tuple[PurePosixPath, ...]:
    """Only folders of payload files pruned WITH their terminal task manifest.

    Never select an orphan directory merely because it exists: claims,
    interrupted publication and remote Git retention remain unresolved.
    """
    manifest_ids = set()
    for relative in paths:
        p = PurePosixPath(relative)
        if (len(p.parts) == 3 and p.parts[:2] == (".agent", "tasks")
                and p.name.endswith(".json")
                and _TASK_ID_RE.fullmatch(p.stem) is not None):
            manifest_ids.add(p.stem)
    candidates: set[PurePosixPath] = set()
    for relative in paths:
        p = PurePosixPath(relative)
        if len(p.parts) < 4 or p.parts[:2] != (".agent", "tasks"):
            continue
        payload_dir = p.parts[2]
        if not payload_dir.endswith(".payload"):
            continue
        if payload_dir[:-8] not in manifest_ids:
            continue
        if _TASK_ID_RE.fullmatch(payload_dir[:-8]) is None:
            continue
        for depth in range(3, len(p.parts)):
            candidates.add(PurePosixPath(*p.parts[:depth]))
    return tuple(sorted(candidates, key=lambda p: (-len(p.parts), str(p))))


def _rmdir_selected_empty_payload_dir(control: Path, relative: PurePosixPath) -> bool:
    """Rmdir only, with a descriptor-walk that cannot traverse symlink parents."""
    if (len(relative.parts) < 3 or relative.parts[:2] != (".agent", "tasks")
            or not relative.parts[2].endswith(".payload")
            or _TASK_ID_RE.fullmatch(relative.parts[2][:-8]) is None
            or any(part in {".", "..", ""} for part in relative.parts)):
        raise ValueError("invalid empty payload directory candidate")
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(control, flags)
    try:
        for component in relative.parts[:-1]:
            next_fd = os.open(component, flags, dir_fd=fd)
            os.close(fd)
            fd = next_fd
        try:
            os.rmdir(relative.name, dir_fd=fd)
        except OSError as exc:
            if exc.errno in {errno.ENOTEMPTY, errno.EEXIST, errno.ENOENT}:
                return False
            raise
        return True
    finally:
        os.close(fd)


def _control_git_environment(core_module: Any) -> Mapping[str, str] | None:
    """Keep repository leases but exclude task-resource FDs from runtime-GC Git."""
    raw = getattr(core_module, "ENV", None)
    if not isinstance(raw, Mapping):
        return None
    environment = dict(raw)
    environment.pop(RESOURCE_LEASE_FDS_ENV, None)
    return environment


def _control_process(
    core_module: Any,
    args: list[str],
    *,
    timeout: int,
    log_commands: bool,
) -> dict[str, Any]:
    kwargs: dict[str, Any] = {
        "timeout": timeout,
        "log_commands": log_commands,
    }
    environment = _control_git_environment(core_module)
    if environment is not None:
        kwargs["environment"] = environment
    return core_module.process(args, core_module.CONTROL, **kwargs)


def _require_control_git(result: dict[str, Any], operation: str) -> str:
    if result["exit_code"] != 0:
        raise RuntimeError(f"{operation} failed: {storage.git_failure_diagnostic(result)}")
    return str(result.get("output", "")).strip()


def _control_output(
    core_module: Any,
    args: list[str],
    operation: str,
    *,
    timeout: int = 30,
) -> str:
    return _require_control_git(
        _control_process(
            core_module,
            ["git", *args],
            timeout=timeout,
            log_commands=False,
        ),
        operation,
    )


def _control_history_policy_enabled(core_module: Any) -> bool:
    messages = _control_output(
        core_module,
        [
            "log",
            "--format=%B",
            "--max-count",
            str(storage.CONTROL_HISTORY_DEPTH),
            "HEAD",
        ],
        "inspect control history policy",
        timeout=30,
    )
    return CONTROL_HISTORY_POLICY_TRAILER in messages


def _parse_remote_branch_sha(output: str, branch: str) -> str:
    lines = [line for line in output.splitlines() if line.strip()]
    if len(lines) != 1:
        raise RuntimeError(
            f"expected exactly one remote ref for {branch!r}, got {len(lines)}"
        )
    fields = lines[0].split()
    expected_ref = f"refs/heads/{branch}"
    if len(fields) < 2 or fields[1] != expected_ref:
        raise RuntimeError(f"unexpected ls-remote output for {branch!r}: {lines[0]!r}")
    return fields[0]


def _remote_control_sha(core_module: Any) -> str:
    branch = core_module.CONTROL_BRANCH
    environment = _control_git_environment(core_module)
    result = storage.run_git_with_network_retry(
        core_module,
        ["git", "ls-remote", "--heads", "origin", f"refs/heads/{branch}"],
        core_module.CONTROL,
        timeout=120,
        log_commands=False,
        environment=environment,
    )
    output = _require_control_git(result, "read remote control branch")
    return _parse_remote_branch_sha(output, branch)


def _fetch_remote_control_tip(core_module: Any) -> str:
    branch = core_module.CONTROL_BRANCH
    remote_ref = f"refs/remotes/origin/{branch}"
    environment = _control_git_environment(core_module)
    fetch = storage.run_git_with_network_retry(
        core_module,
        [
            "git",
            "fetch",
            "--depth",
            str(storage.CONTROL_HISTORY_DEPTH),
            "--no-tags",
            "origin",
            f"+refs/heads/{branch}:{remote_ref}",
        ],
        core_module.CONTROL,
        timeout=120,
        log_commands=False,
        environment=environment,
    )
    _require_control_git(fetch, "fetch remote control branch for reconciliation")
    return _control_output(
        core_module,
        ["rev-parse", remote_ref],
        "read reconciled remote control tip",
    )


def _is_ancestor(core_module: Any, ancestor: str, descendant: str) -> bool:
    result = _control_process(
        core_module,
        ["git", "merge-base", "--is-ancestor", ancestor, descendant],
        timeout=30,
        log_commands=False,
    )
    if result["exit_code"] == 0:
        return True
    if result["exit_code"] == 1:
        return False
    raise RuntimeError(storage.git_failure_diagnostic(result))


def _realign_control_checkout(
    core_module: Any,
    *,
    target_sha: str,
    expected_tree: str | None = None,
) -> None:
    branch = core_module.CONTROL_BRANCH
    reset = _control_process(
        core_module,
        ["git", "reset", "--hard", target_sha],
        timeout=120,
        log_commands=False,
    )
    _require_control_git(reset, "reset compacted control checkout")
    update_ref = _control_process(
        core_module,
        ["git", "update-ref", f"refs/remotes/origin/{branch}", target_sha],
        timeout=30,
        log_commands=False,
    )
    _require_control_git(update_ref, "update compacted remote-tracking ref")

    final_head = _control_output(core_module, ["rev-parse", "HEAD"], "verify compacted HEAD")
    if final_head != target_sha:
        raise RuntimeError("local control checkout did not converge on reconciled remote tip")
    if expected_tree is not None:
        final_tree = _control_output(
            core_module,
            ["rev-parse", "HEAD^{tree}"],
            "verify compacted control tree",
        )
        if final_tree != expected_tree:
            raise RuntimeError("local control tree changed during history compaction")


def _compact_control_history_locked(
    core_module: Any,
    *,
    threshold: int = CONTROL_HISTORY_COMPACTION_THRESHOLD,
) -> dict[str, Any]:
    """Bound remote control history while preserving the exact current tree.

    Caller must hold CONTROL_GIT_LOCK. The force push is never blindly retried: an
    ambiguous transport failure is reconciled against the remote ref first.
    """
    if threshold < 1:
        raise ValueError("control history compaction threshold must be positive")
    storage.require_control_rebase_clear(core_module.CONTROL)

    dirty = _control_output(
        core_module,
        ["status", "--porcelain=v1", "--untracked-files=all"],
        "inspect control checkout",
    )
    if dirty:
        return {"changed": False, "reason": "dirty"}

    branch = _control_output(
        core_module,
        ["symbolic-ref", "--quiet", "--short", "HEAD"],
        "read control branch",
    )
    if branch != core_module.CONTROL_BRANCH:
        return {"changed": False, "reason": "wrong_branch", "branch": branch}

    visible_raw = _control_output(
        core_module,
        ["rev-list", "--count", "HEAD"],
        "count visible control commits",
    )
    try:
        visible_commits = int(visible_raw)
    except ValueError as exc:
        raise RuntimeError(f"invalid control commit count: {visible_raw!r}") from exc
    if visible_commits < threshold:
        return {
            "changed": False,
            "reason": "below_threshold",
            "visible_commits": visible_commits,
            "threshold": threshold,
        }
    if not _control_history_policy_enabled(core_module):
        return {
            "changed": False,
            "reason": "history_policy_unmanaged",
            "visible_commits": visible_commits,
            "threshold": threshold,
        }

    old_sha = _control_output(core_module, ["rev-parse", "HEAD"], "read control HEAD")
    remote_sha = _remote_control_sha(core_module)
    if old_sha != remote_sha:
        return {
            "changed": False,
            "reason": "remote_changed",
            "head_sha": old_sha,
            "remote_sha": remote_sha,
        }

    tree_sha = _control_output(
        core_module,
        ["rev-parse", "HEAD^{tree}"],
        "read control tree",
    )
    new_sha = _control_output(
        core_module,
        ["commit-tree", tree_sha, "-m", CONTROL_HISTORY_COMPACT_ROOT_MESSAGE],
        "create compacted control root commit",
        timeout=60,
    )
    new_tree = _control_output(
        core_module,
        ["rev-parse", f"{new_sha}^{{tree}}"],
        "verify compacted root tree",
    )
    if new_tree != tree_sha:
        raise RuntimeError(
            "compacted control tree differs from source tree; refusing remote update"
        )

    lease = f"--force-with-lease=refs/heads/{branch}:{old_sha}"
    push = _control_process(
        core_module,
        ["git", "push", lease, "origin", f"{new_sha}:refs/heads/{branch}"],
        timeout=120,
        log_commands=False,
    )
    push_succeeded = push["exit_code"] == 0
    reconciled_tip = new_sha
    remote_advanced = False
    if not push_succeeded:
        observed_remote = _remote_control_sha(core_module)
        if observed_remote != new_sha:
            fetched_remote = _fetch_remote_control_tip(core_module)
            if _is_ancestor(core_module, new_sha, fetched_remote):
                reconciled_tip = fetched_remote
                remote_advanced = fetched_remote != new_sha
            else:
                return {
                    "changed": False,
                    "reason": "lease_or_push_failed",
                    "old_sha": old_sha,
                    "new_sha": new_sha,
                    "remote_sha": fetched_remote,
                    "diagnostic": storage.git_failure_diagnostic(push),
                }

    _realign_control_checkout(
        core_module,
        target_sha=reconciled_tip,
        expected_tree=tree_sha if reconciled_tip == new_sha else None,
    )
    if reconciled_tip != new_sha and not _is_ancestor(core_module, new_sha, reconciled_tip):
        raise RuntimeError("reconciled control tip does not descend from compacted root")
    return {
        "changed": True,
        "old_sha": old_sha,
        "new_sha": new_sha,
        "head_sha": reconciled_tip,
        "tree_sha": tree_sha,
        "visible_commits_before": visible_commits,
        "push_reconciled": not push_succeeded,
        "remote_advanced": remote_advanced,
    }


def compact_control_history(
    core_module: Any,
    *,
    threshold: int = CONTROL_HISTORY_COMPACTION_THRESHOLD,
) -> dict[str, Any]:
    """Public lock-safe control-history compaction entry point."""
    with core_module.CONTROL_GIT_LOCK:
        return _compact_control_history_locked(core_module, threshold=threshold)


def control_cleanup_plan(
    control: Path,
    *,
    terminal_pair_retention: int = TERMINAL_PAIR_RETENTION,
    run_retention: int = RUN_RETENTION,
    ack_retention: int = ACK_RETENTION,
    orphan_result_retention: int = ORPHAN_RESULT_RETENTION,
) -> tuple[str, ...]:
    """Return runtime paths safe to delete without making a task replayable."""
    if min(
        terminal_pair_retention,
        run_retention,
        ack_retention,
        orphan_result_retention,
    ) < 0:
        raise ValueError("runtime retention values must be non-negative")

    tasks_dir = control / ".agent/tasks"
    results_dir = control / ".agent/results"
    runs_dir = control / ".agent/runs"
    acks_dir = control / ".agent/daemon/acks"
    control_request = control / ".agent/daemon/control.json"

    deletes: set[Path] = set()
    pending_ids: set[str] = set()
    referenced_results: set[Path] = set()
    terminal_pairs: list[tuple[tuple[float, str], Path, Path, str]] = []

    for task_path in _json_files(tasks_dir):
        task = _read_json(task_path)
        raw_id = task.get("id")
        task_id = (
            raw_id
            if isinstance(raw_id, str) and _TASK_ID_RE.fullmatch(raw_id) is not None
            else task_path.stem
        )
        result_path = results_dir / f"{task_id}.json"
        if not result_path.exists() and task_id != task_path.stem:
            alias_result = results_dir / f"{task_path.stem}.json"
            if alias_result.exists():
                result_path = alias_result
        if result_path.exists():
            referenced_results.add(result_path)
            terminal_pairs.append(
                (_sort_key(result_path), task_path, result_path, task_id)
            )
        else:
            pending_ids.add(task_id)
            pending_ids.add(task_path.stem)

    terminal_pairs.sort(key=lambda item: item[0], reverse=True)
    for _key, task_path, result_path, task_id in terminal_pairs[
        terminal_pair_retention:
    ]:
        payload_files = _task_payload_files(tasks_dir, task_id)
        if payload_files is None:
            # Do not prune terminal records if doing so would strand unknown
            # payload files or delete data the canonical writer cannot create.
            continue
        deletes.add(task_path)
        deletes.add(result_path)
        deletes.update(payload_files)

    orphan_results = [
        path for path in _json_files(results_dir) if path not in referenced_results
    ]
    orphan_results.sort(key=_sort_key, reverse=True)
    deletes.update(orphan_results[orphan_result_retention:])

    run_candidates = [
        path for path in _json_files(runs_dir) if path.stem not in pending_ids
    ]
    run_candidates.sort(key=_sort_key, reverse=True)
    deletes.update(run_candidates[run_retention:])

    protected_ack: str | None = None
    request = _read_json(control_request)
    request_id = request.get("id")
    if isinstance(request_id, str) and request_id:
        protected_ack = request_id

    ack_candidates = [
        path for path in _json_files(acks_dir) if path.stem != protected_ack
    ]
    ack_candidates.sort(key=_sort_key, reverse=True)
    deletes.update(ack_candidates[ack_retention:])

    return tuple(sorted(_relative(control, path) for path in deletes))


def prune_control_runtime(core_module: Any) -> dict[str, Any]:
    """Prune runtime artifacts and keep remote control history bounded."""
    if not (core_module.CONTROL / ".git").exists():
        return {
            "changed": False,
            "deleted": 0,
            "paths": (),
            "history": {"changed": False, "reason": "not_git_checkout"},
        }

    paths: tuple[str, ...] = ()
    with core_module.CONTROL_GIT_LOCK:
        storage.require_control_rebase_clear(core_module.CONTROL)
        paths = control_cleanup_plan(core_module.CONTROL)
        if paths:
            with termination_critical_section():
                for relative in paths:
                    relative_path = PurePosixPath(relative)
                    if (
                        relative_path.is_absolute()
                        or relative_path.as_posix() != relative
                        or any(part in {"", ".", ".."} for part in relative_path.parts)
                    ):
                        raise ValueError(f"cleanup path is non-canonical: {relative!r}")
                    candidate = core_module.CONTROL / relative
                    root = core_module.CONTROL.resolve()
                    resolved_parent = candidate.parent.resolve()
                    if resolved_parent != root and root not in resolved_parent.parents:
                        raise ValueError(f"cleanup path escapes control checkout: {relative!r}")
                    if not any(relative.startswith(prefix) for prefix in _RUNTIME_PREFIXES):
                        raise ValueError(f"cleanup path is outside runtime allowlist: {relative!r}")
                    candidate.unlink(missing_ok=True)

                # A terminal-pair GC plan removes payload files but Python's
                # unlink leaves directory-only residue. Prune ONLY directories
                # from that same selected terminal pair, deepest first.
                # rmdir cannot remove a nonempty tree; O_NOFOLLOW on each
                # parent prevents removing directories through symlink routes.
                for directory in _selected_empty_payload_directories(paths):
                    _rmdir_selected_empty_payload_dir(core_module.CONTROL, directory)

                add = _control_process(
                    core_module,
                    ["git", "add", "-A", "--", *paths],
                    timeout=30,
                    log_commands=False,
                )
                if add["exit_code"] != 0:
                    raise RuntimeError(storage.git_failure_diagnostic(add))

                staged = _control_process(
                    core_module,
                    ["git", "diff", "--cached", "--quiet", "--", *paths],
                    timeout=30,
                    log_commands=False,
                )
                if staged["exit_code"] not in {0, 1}:
                    raise RuntimeError(storage.git_failure_diagnostic(staged))

                if staged["exit_code"] == 1:
                    commit = _control_process(
                        core_module,
                        [
                            "git",
                            "commit",
                            "-m",
                            f"Agent runtime GC: prune {len(paths)} artifacts",
                            "--",
                            *paths,
                        ],
                        timeout=60,
                        log_commands=False,
                    )
                    if commit["exit_code"] != 0:
                        raise RuntimeError(storage.git_failure_diagnostic(commit))

                    environment = _control_git_environment(core_module)
                    # The lock excludes Local Agent writers, but external Git
                    # processes can still start a rebase during local GC.
                    storage.require_control_rebase_clear(core_module.CONTROL)
                    pull = storage.run_git_with_network_retry(
                        core_module,
                        ["git", *storage.bounded_control_pull_args(core_module.CONTROL_BRANCH)],
                        core_module.CONTROL,
                        timeout=120,
                        log_commands=False,
                        environment=environment,
                    )
                    if pull["exit_code"] != 0:
                        raise RuntimeError(pull["output"])

                    push = storage.run_git_with_network_retry(
                        core_module,
                        ["git", "push", "origin", core_module.CONTROL_BRANCH],
                        core_module.CONTROL,
                        timeout=120,
                        log_commands=False,
                        environment=environment,
                    )
                    if push["exit_code"] != 0:
                        raise RuntimeError(push["output"])

        try:
            history = _compact_control_history_locked(core_module)
        except Exception as exc:
            history = {
                "changed": False,
                "reason": "error",
                "error": f"{type(exc).__name__}: {exc}",
            }

    logger = getattr(core_module, "log", None)
    if callable(logger):
        if paths:
            logger(f"runtime GC pruned {len(paths)} control artifacts")
        if history.get("changed"):
            logger(
                "runtime GC compacted control history "
                f"from {history['visible_commits_before']} visible commits"
            )
        elif history.get("reason") == "error":
            logger(f"control history compaction skipped: {history['error']}")

    return {
        "changed": bool(paths) or bool(history.get("changed")),
        "deleted": len(paths),
        "paths": paths,
        "history": history,
    }
