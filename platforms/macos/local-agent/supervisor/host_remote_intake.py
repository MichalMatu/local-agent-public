"""Exact-snapshot preview and locked checkout refresh for machine commands.

Preview is read-only. Refresh only fast-forwards a clean dedicated checkout;
neither operation claims, stages, executes or publishes tasks.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from local_agent.operator.remote import (
    _remote_ref, fetch_operator_host_command_grants, fetch_operator_host_registry,
)
from local_agent.repository.host_execution import load_local_operator_pin
from local_agent.repository.host_facts import load_local_host_identity
from local_agent.repository.host_mailbox_reader import _blob_from_snapshot, _git, _json_object
from local_agent.runtime.task_contract import task_digest
from local_agent.supervisor.host_control import HostControlSource, validate_host_control_checkout
from local_agent.supervisor.host_remote_grants import require_exact_host_command_grant
from local_agent.supervisor.host_tasks import (
    MAX_LOCAL_REQUEST_BYTES, _TASK_ID_RE, validate_local_host_task,
)

_GIT_SHA = re.compile(r"^[0-9a-f]{40}$")
_CONTROL_HEAD = "refs/heads/agent-control"


class HostControlSnapshotChanged(RuntimeError):
    """A concurrent remote writer invalidated a preview; retry before effects."""


def refresh_host_control_checkout(
    source: HostControlSource, *, production_checkout: Path,
) -> None:
    """Fast-forward once under the caller's host_supervisor_lease.

    Preserve dirty work, unpublished local commits and interrupted Git state.
    A later remote writer is detected by the independent read-only preview.
    """
    from local_agent.foundation.storage import require_control_rebase_clear

    validate_host_control_checkout(source, production_checkout=production_checkout)
    checkout = source.checkout
    require_control_rebase_clear(checkout)
    if _git(checkout, "status", "--porcelain", "--untracked-files=all").strip():
        raise ValueError("host control checkout has local changes; refusing refresh")
    _git(checkout, "fetch", "--no-tags", "--no-recurse-submodules", "origin", _CONTROL_HEAD)
    snapshot = _git(checkout, "rev-parse", "--verify", "FETCH_HEAD^{commit}").strip()
    if _GIT_SHA.fullmatch(snapshot) is None:
        raise ValueError("cannot establish fetched host control commit")
    _git(checkout, "merge-base", "--is-ancestor", "HEAD", snapshot)
    _git(checkout, "merge", "--ff-only", "--no-edit", snapshot)
    if _git(checkout, "rev-parse", "HEAD").strip() != snapshot:
        raise ValueError("host control checkout changed during refresh")


@dataclass(frozen=True, slots=True)
class RemoteHostTaskPreview:
    task: dict[str, Any]
    task_digest: str
    control_sha: str
    operator_sha: str
    task_id: str
    validation_error: str | None = None


def preview_authorized_remote_host_task(
    source: HostControlSource,
    *,
    task_id: str,
    identity_file: Path,
    production_checkout: Path,
    operator_checkout: Path,
    authorization: str = "operator_grants",
) -> RemoteHostTaskPreview:
    """Bind remote bytes to the intended machine and selected Git snapshot.

    The owner may select GitHub-writer authority once in the local private
    host intake config. Legacy operator-grant installations retain their
    existing independent pinned policy until explicitly migrated.
    """
    if not isinstance(source, HostControlSource):
        raise ValueError("invalid host control source")
    if authorization not in ("operator_grants", "github_writers"):
        raise ValueError("unsupported host task authorization model")
    if not isinstance(task_id, str) or _TASK_ID_RE.fullmatch(task_id) is None:
        raise ValueError("invalid machine task ID")
    identity = load_local_host_identity(identity_file)
    if identity.host_id != source.host_id or identity.installation_id != source.installation_id:
        raise ValueError("local host identity differs from selected control source")
    validate_host_control_checkout(source, production_checkout=production_checkout)
    if authorization == "operator_grants":
        pin = load_local_operator_pin(identity_file)
        operator_sha, enrollments = fetch_operator_host_registry(operator_checkout)
        grant_sha, grants = fetch_operator_host_command_grants(operator_checkout)
        if pin != operator_sha or pin != grant_sha or _remote_ref(operator_checkout) != pin:
            raise ValueError("host command operator approvals are unverified or changed")
        if not any(item.enabled and item.identity == identity for item in enrollments):
            raise ValueError("operator has not enrolled this installation")
    else:
        # Repository access is managed by GitHub/the owner, not by a second
        # per-task operator-approval service. The local intake config is the
        # explicit machine-side choice; immutable target checks still apply.
        pin = "github_writers"
        grants = ()

    checkout = source.checkout
    remote_head = _git(checkout, "ls-remote", "--heads", "origin", _CONTROL_HEAD).strip()
    pieces = remote_head.split()
    if len(pieces) != 2 or pieces[1] != _CONTROL_HEAD or _GIT_SHA.fullmatch(pieces[0]) is None:
        raise ValueError("cannot establish exact remote host control HEAD")
    control_sha = pieces[0]
    local_head = _git(checkout, "rev-parse", "HEAD").strip()
    if local_head != control_sha:
        raise HostControlSnapshotChanged("host control checkout is not current with remote HEAD")
    path = PurePosixPath(f"{source.namespace}/tasks/{task_id}.json")
    raw = _blob_from_snapshot(checkout, control_sha, path, limit=MAX_LOCAL_REQUEST_BYTES)
    manifest = _json_object(raw)
    if manifest.get("id") != task_id:
        raise ValueError("host command filename/task ID mismatch")
    if (manifest.get("target_host_id") != identity.host_id or
            manifest.get("target_installation_id") != identity.installation_id):
        raise ValueError("host task targets a different installation")
    digest = task_digest(manifest)
    if authorization == "operator_grants":
        require_exact_host_command_grant(
            identity=identity, task_id=task_id, task_digest=digest, grants=grants,
        )
    # Only an authorized, exactly targeted request can receive a terminal
    # validation result. Transport/authority failures remain retryable errors.
    validation_error = None
    try:
        validate_local_host_task(manifest, identity)
    except ValueError as exc:
        validation_error = f"{type(exc).__name__}: {exc}"[:2000]
    if authorization == "operator_grants" and _remote_ref(operator_checkout) != pin:
        raise ValueError("host command operator approval changed during inspection")
    if _git(checkout, "ls-remote", "--heads", "origin", _CONTROL_HEAD).strip() != remote_head:
        raise HostControlSnapshotChanged("remote host command Git task changed during inspection")
    return RemoteHostTaskPreview(
        task=manifest, task_digest=digest, control_sha=control_sha,
        operator_sha=pin, task_id=task_id,
        validation_error=validation_error,
    )
