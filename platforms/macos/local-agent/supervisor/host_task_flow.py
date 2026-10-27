"""High-level owner host submission over the existing Git producer.

One ordinary call handles the familiar safe case where result publications
advanced the remote branch. It never resets, rebases, force pushes, retries
an uncertain publish, executes host commands, or changes local enrollment.
A clean isolated producer is required for automatic fast-forward.
"""
from __future__ import annotations

import re
from pathlib import Path, PurePosixPath
from typing import Any

from local_agent.repository.host_mailbox_reader import _blob_from_snapshot, _git, _json_object
from local_agent.repository.host_routing import HostIdentity
from local_agent.runtime.task_contract import task_digest, validate_task
from local_agent.supervisor.host_control import CONTROL_BRANCH
from local_agent.supervisor.host_private_producer import (
    load_private_host_producer, sync_private_host_producer,
)
from local_agent.supervisor.host_remote_client import (
    publish_staged_host_command, read_host_command_result, stage_host_command,
)
from local_agent.supervisor.host_tasks import (
    MAX_LOCAL_REQUEST_BYTES, _TASK_ID_RE, validate_local_host_task,
)

_SHA = re.compile(r"^[0-9a-f]{40}$")


def submit_host_command(profile: Path, task: dict[str, Any]) -> dict[str, Any]:
    """Submit one immutable host task, auto-syncing only an intact clean producer.

    A network race or uncertain push leaves evidence in the isolated checkout
    for inspection, rather than silently retrying effects or dropping a task.
    """
    source = load_private_host_producer(Path(profile))
    identity = HostIdentity(source.host_id, source.installation_id)
    normalized, _ = validate_local_host_task(task, identity)
    validate_task(normalized, allow_local_codex=True)
    remote_sha, synced = _fresh_private_head(Path(profile), source)
    checkout = source.checkout
    # Existing low-level methods are the only writer and publisher.
    staged = stage_host_command(
        task, host_id=source.host_id, installation_id=source.installation_id,
        output_root=checkout,
    )
    result = publish_staged_host_command(
        source, task_id=task["id"],
        expected_control_sha=remote_sha,
        expected_task_digest=staged["task_digest"],
    )
    return {**result, "synced_prior_results": synced}


def _fresh_private_head(profile: Path, source: Any) -> tuple[str, bool]:
    """Reconcile only a clean isolated producer, with exact remote evidence."""
    checkout = source.checkout
    if _git(checkout, "status", "--porcelain=v1", "--untracked-files=all").strip():
        raise ValueError("host producer is not clean; preserve and inspect pending task")
    local_sha = _git(checkout, "rev-parse", "HEAD").strip()
    ref = f"refs/heads/{CONTROL_BRANCH}"
    remote_record = _git(checkout, "ls-remote", "--heads", "origin", ref).strip()
    parts = remote_record.split()
    if (_SHA.fullmatch(local_sha) is None or len(parts) != 2
            or parts[1] != ref or _SHA.fullmatch(parts[0]) is None):
        raise ValueError("cannot establish exact host Git source")
    remote_sha = parts[0]
    changed = local_sha != remote_sha
    if changed:
        sync_private_host_producer(
            profile, expected_local_sha=local_sha,
            expected_remote_sha=remote_sha,
        )
    return remote_sha, changed


def current_host_command_result(profile: Path, task_id: str) -> dict[str, Any]:
    """Read the current result of a published immutable task, without manual SHA.

    A missing terminal result means pending, not permission to rerun commands.
    Legacy producer pin constraints remain in the existing sync primitive.
    """
    if not isinstance(task_id, str) or _TASK_ID_RE.fullmatch(task_id) is None:
        raise ValueError("invalid host task ID")
    source = load_private_host_producer(Path(profile), verify_authority=False)
    sha, _ = _fresh_private_head(Path(profile), source)
    identity = HostIdentity(source.host_id, source.installation_id)
    task_path = PurePosixPath(f"{source.namespace}/tasks/{task_id}.json")
    task = _json_object(_blob_from_snapshot(
        source.checkout, sha, task_path, limit=MAX_LOCAL_REQUEST_BYTES,
    ))
    validate_local_host_task(task, identity)
    if task.get("id") != task_id:
        raise ValueError("published host task ID mismatch")
    digest = task_digest(task)
    target = source.checkout / source.namespace / "task-results" / f"{task_id}.json"
    if target.is_symlink():
        raise ValueError("host result cannot be a symlink")
    if not target.exists():
        return {"status": "pending", "task_id": task_id, "task_digest": digest,
                "source_sha": sha, "execution_authority": "not_asserted"}
    return read_host_command_result(
        source, task_id=task_id, expected_control_sha=sha,
        expected_task_digest=digest,
    )
