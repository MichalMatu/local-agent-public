"""Disposable Git claim-CAS simulation; never a task claim or execution adapter.

Exercises optimistic serialization on one repository's *shared* control ref.
No credential, authenticated enrollment, distributed execution fence or real
lease is established here. In particular, an ambiguous write is UNKNOWN and
must never be automatically replayed, even with identical task bytes.
"""

from __future__ import annotations

import json
import re
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any

from local_agent.repository.host_mailbox_reader import (
    _blob_from_snapshot, _json_object, _payload_paths,
)
from local_agent.repository.host_routing import (
    HostEnrollment, HostIdentity, host_task_path, require_host_task_target,
)
from local_agent.repository.host_status_cas import (
    _CONTROL_REF, _git, _require_fixture_root,
)
from local_agent.runtime.task_contract import MAX_TASK_FILE_BYTES, task_digest, validate_task

_SHA = re.compile(r"^[0-9a-f]{40}$")
_DIGEST = re.compile(r"^[0-9a-f]{64}$")


def simulate_host_claim_cas(
    fixture_root: Path,
    bare_remote: Path,
    *,
    expected_control_sha: str,
    expected_operator_sha: str,
    expected_task_digest: str,
    task_id: str,
    local_identity: HostIdentity,
    enrollment: HostEnrollment,
    repository_id: str,
    agent_binding: str,
    operator_disabled: bool = False,
    lifecycle_state: str = "queued",
) -> dict[str, Any]:
    """Stage one *fixture-only* claim and attempt exactly one guarded Git push.

    All Git writes go to a bare repository inside an explicitly marked,
    caller-owned disposable directory. This helper is never called by workers.
    """
    if operator_disabled:
        raise ValueError("operator_emergency_stop")
    if lifecycle_state != "queued":
        raise ValueError("host_task_not_replayable")
    if (not isinstance(local_identity, HostIdentity)
            or not isinstance(enrollment, HostEnrollment)
            or not enrollment.enabled or enrollment.identity != local_identity
            or not isinstance(repository_id, str) or re.fullmatch(r"[A-Za-z0-9._-]{1,100}", repository_id) is None
            or not isinstance(agent_binding, str)
            or (repository_id.casefold(), agent_binding) not in {
                (name.casefold(), binding) for name, binding in enrollment.repositories
            }
            or any(not isinstance(value, str) or _SHA.fullmatch(value) is None for value in (
                expected_control_sha, expected_operator_sha,
            ))
            or not isinstance(expected_task_digest, str)
            or _DIGEST.fullmatch(expected_task_digest) is None):
        raise ValueError("claim fixture requires valid pinned enrollment, binding and revisions")
    manifest_path = host_task_path(local_identity.host_id, task_id)
    # All claimants of this repository-scoped task use the same path/ref. A
    # per-host local process lock must not be mistaken for cross-host fencing.
    claim_path = PurePosixPath(".agent/claims/host-targeted") / f"{task_id}.json"
    root, bare = _require_fixture_root(Path(fixture_root), Path(bare_remote))
    if _git(bare, "rev-parse", "--verify", _CONTROL_REF) != expected_control_sha:
        raise ValueError("fixture claim expected control SHA changed")

    with tempfile.TemporaryDirectory(prefix="host-claim-cas-", dir=root) as directory:
        work = Path(directory) / "work"
        _git(root, "clone", "--no-local", "--quiet", str(bare), str(work))
        if _git(work, "rev-parse", "HEAD") != expected_control_sha:
            raise ValueError("fixture claim source checkout changed")
        raw = _blob_from_snapshot(work, expected_control_sha, manifest_path,
                                  limit=MAX_TASK_FILE_BYTES)
        task = _json_object(raw)
        if (task.get("id") != task_id
                or type(task.get("host_mailbox_version")) is not int
                or task["host_mailbox_version"] != 1):
            raise ValueError("fixture claim task identity or schema is invalid")
        require_host_task_target(
            task, local_identity=local_identity, enrollment=enrollment,
            repository=_FixtureTarget(repository_id, agent_binding),
        )
        validate_task(task, require_agent_binding=True)
        if task_digest(task) != expected_task_digest:
            raise ValueError("fixture claim immutable task digest changed")
        # This fixture only models small inline tasks; sidecars must be proven
        # separately by the pinned mailbox reader before a future real intake.
        if _payload_paths(task):
            raise ValueError("fixture claim requires inline task without payload sidecars")
        prior = _git(work, "ls-tree", expected_control_sha, "--", claim_path.as_posix(), max_bytes=1024)
        if prior:
            raise ValueError("fixture claim exists; never replay")
        target = work / claim_path
        if any(part.is_symlink() for part in (target, *target.parents)) or target.exists():
            raise ValueError("fixture claim path is unsafe or already exists")
        target.parent.mkdir(parents=True, exist_ok=True)
        claim = {
            "version": 1, "state": "claimed_fixture_only", "task_id": task_id,
            "task_digest": expected_task_digest,
            "repository_id": repository_id, "agent_binding": agent_binding,
            "host_id": local_identity.host_id,
            "installation_id": local_identity.installation_id,
            "operator_registry_sha": expected_operator_sha,
            "task_source_sha": expected_control_sha,
            "execution_authority": "not_asserted",
        }
        target.write_text(json.dumps(claim, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")
        _git(work, "add", "--", claim_path.as_posix())
        if _git(work, "diff", "--cached", "--name-only") != claim_path.as_posix():
            raise ValueError("fixture claim staged unexpected paths")
        _git(work, "-c", "user.name=Offline Claim Fixture", "-c",
             "user.email=offline@example.invalid", "commit", "--quiet", "-m",
             "test: one-shot disposable Git claim CAS", "--", claim_path.as_posix())
        committed = _git(work, "rev-parse", "HEAD")
        if _git(bare, "rev-parse", "--verify", _CONTROL_REF) != expected_control_sha:
            raise ValueError("fixture claim concurrent writer advanced control ref")
        try:
            _git(work, "push", "--porcelain",
                 f"--force-with-lease={_CONTROL_REF}:{expected_control_sha}",
                 "origin", f"HEAD:{_CONTROL_REF}")
            # Lack of post-push acknowledgement is UNKNOWN: do not retry, even
            # if a later observer can find a matching commit or claim record.
            if _git(bare, "rev-parse", "--verify", _CONTROL_REF) != committed:
                raise ValueError("fixture claim acknowledgement uncertain")
        except ValueError as exc:
            raise ValueError("fixture claim outcome UNKNOWN; never auto-retry") from exc
    return {
        "status": "offline_fixture_only", "published_to_production": False,
        "execution_authority": "not_asserted", "claimed_in_production": False,
        "executed": False, "previous_sha": expected_control_sha,
        "fixture_commit_sha": committed, "changed_path": claim_path.as_posix(),
        "task_digest": expected_task_digest,
    }


class _FixtureTarget:
    """Only the repository fields required by the pure binding checker."""

    def __init__(self, repository_id: str, agent_binding: str) -> None:
        self.repository_id = repository_id
        self.agent_binding = agent_binding
