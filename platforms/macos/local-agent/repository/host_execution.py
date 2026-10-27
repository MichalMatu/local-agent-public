"""Opt-in operator-authorized mailbox admission for a single repository owner.

Uses the existing GitHub operator-control reader and existing task executor.
No second scheduler, identity-only grants, cached permission, or shared-host
fencing; a transient registry failure leaves the addressed task unclaimed.
"""

from __future__ import annotations

import os
import re
import stat
from pathlib import Path
from typing import Any

from local_agent.daemon.service import load_task_file
from local_agent.operator.remote import fetch_operator_host_registry, _remote_ref
from local_agent.paths import repository_root
from local_agent.repository.context import RepositoryContext
from local_agent.repository.host_admission import (
    HostAdmissionBlocked, has_host_target, preflight_host_targeted_task,
)
from local_agent.repository.host_facts import load_local_host_identity
from local_agent.repository.host_routing import reject_host_namespace_symlinks
from local_agent.repository.path_safety import has_untrusted_symlink
from local_agent.runtime.task_contract import task_digest


class HostAdmissionUnavailable(RuntimeError):
    """Enrollment cannot presently be proven; keep task pending, no result."""


_OPERATOR_SHA_RE = re.compile(r"[0-9a-f]{40}\n?\Z")


def load_local_operator_pin(identity_path: Path) -> str:
    """Read an operator-provisioned immutable revision outside writable Git.

    Repository-scoped GitHub deploy keys are NOT branch-scoped. Without this
    machine-local pin, a key allowed to publish results could also forge a
    new ``operator-control`` enrollment for itself. A changed remote ref must
    pause admission until the operator reapproves its exact SHA locally.
    """
    path = identity_path.parent / "operator-control.sha"
    if not path.is_absolute() or has_untrusted_symlink(path):
        raise ValueError("operator pin path must be absolute without symlinks")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    with os.fdopen(os.open(path, flags), "rb") as handle:
        metadata = os.fstat(handle.fileno())
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_mode & 0o077:
            raise ValueError("operator pin must be a private regular file")
        if os.name == "posix" and metadata.st_uid != os.getuid():
            raise ValueError("operator pin owner does not match current user")
        raw = handle.read(42)
    try:
        decoded = raw.decode("ascii")
    except UnicodeDecodeError as exc:
        raise ValueError("operator pin must contain an exact Git SHA") from exc
    if not _OPERATOR_SHA_RE.fullmatch(decoded):
        raise ValueError("operator pin must contain an exact Git SHA")
    return decoded.strip()


def require_host_execution(
    repository: RepositoryContext,
    task: dict[str, Any],
    task_path: Path,
) -> None:
    """Recheck current operator authority and untouched task at intake time.

    A single enabled owner per writable repository is validated by the
    operator registry parser. Cloned credentials/identities are unsupported;
    this check is not a distributed lease and is not safe for multiple owners.
    """
    if (not repository.host_execution_enabled or not repository.host_mailbox_enabled
            or repository.host_id is None or repository.host_identity_file is None):
        raise HostAdmissionUnavailable("host execution is not locally enabled")
    if repository.control_branch != "agent-control":
        raise HostAdmissionUnavailable("host execution requires canonical control branch")

    relative = Path(f".agent/hosts/{repository.host_id}/tasks/{task.get('id')}.json")
    expected = repository.control / relative
    # The task reader canonicalizes its control root. Admit only that exact
    # canonical spelling or the original configured spelling, never another
    # user-controlled alias to the same file.
    if (task_path not in (expected, repository.control.resolve() / relative)
            or has_untrusted_symlink(repository.control)
            or has_untrusted_symlink(task_path)):
        raise HostAdmissionBlocked("host_task_identity_changed")
    try:
        reject_host_namespace_symlinks(repository.control, relative)
        if task_path.is_symlink():
            raise ValueError("symlinked host task file")
        reread = load_task_file(task_path)
        if task_digest(reread) != task_digest(task):
            raise HostAdmissionBlocked("host_task_digest_changed")
    except HostAdmissionBlocked:
        raise
    except (ValueError, OSError) as exc:
        raise HostAdmissionBlocked("host_task_identity_changed") from exc

    try:
        identity_path = repository.host_identity_file
        # Identity must never come from a checkout that remote tasks can modify.
        if any((parent / ".git").exists() for parent in (identity_path.parent, *identity_path.parents)):
            raise ValueError("local host identity cannot live inside a Git checkout")
        identity = load_local_host_identity(identity_path)
        if identity.host_id != repository.host_id:
            raise ValueError("selected mailbox differs from local identity")
        pinned_sha = load_local_operator_pin(identity_path)
        operator_sha, enrollments = fetch_operator_host_registry(repository_root())
        if operator_sha != pinned_sha:
            raise RuntimeError("operator registry is not locally approved")
        # Enrollment is read from an exact remote ref. A changed ref forces a
        # fresh decision rather than relying on a stale grant.
        if _remote_ref(repository_root()) != pinned_sha:
            raise RuntimeError("operator registry advanced during admission")
        owner = next((row for row in enrollments if row.identity == identity), None)
        if owner is None or not owner.enabled:
            raise ValueError("host not enrolled or disabled")
        if (repository.repository_id.casefold(), repository.agent_binding) not in {
            (name.casefold(), binding) for name, binding in owner.repositories
        }:
            raise ValueError("repository ownership is not granted to this host")
        # validate_host_enrollments() forbids active repo/binding collisions;
        # a single verified owner is the bounded MVP execution model.
    except (ValueError, RuntimeError, OSError, UnicodeError) as exc:
        raise HostAdmissionUnavailable("operator enrollment unverified") from exc
    preflight_host_targeted_task(
        task, repository, source="host_mailbox", enabled=True,
        local_identity=identity, enrollment=owner,
        expected_task_id=task_path.stem, expected_task_digest=task_digest(reread),
        single_owner_verified=True,
    )


def check_host_task_admission(
    repository: RepositoryContext,
    task: dict[str, Any],
    task_path: Path,
    *,
    operator_disabled: bool = False,
) -> None:
    """Share the same legacy/host gate across serial and parallel workers.

    A valid host mailbox is never a fallback to legacy admission. An
    unavailable operator grant pauses intake; malformed target intent is a
    terminal refusal. The caller owns result publication and scheduling.
    """
    if operator_disabled:
        raise HostAdmissionUnavailable("operator disabled")
    targeted = has_host_target(task)
    if repository.host_mailbox_enabled and not targeted:
        raise HostAdmissionBlocked("host_target_missing")
    if not targeted:
        return
    if repository.host_mailbox_enabled and repository.host_execution_enabled:
        require_host_execution(repository, task, task_path)
        return
    preflight_host_targeted_task(
        task,
        repository,
        source="host_mailbox" if repository.host_mailbox_enabled else "legacy_queue",
        enabled=False,
        operator_disabled=operator_disabled,
    )
