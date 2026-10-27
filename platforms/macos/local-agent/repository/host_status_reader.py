"""Read one host status from a pinned Git control commit without task admission.

The record is observational only: Git ref, enrollment and local identity checks
are not proof that a separately credentialed host published the record.
"""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path, PurePosixPath

from local_agent.repository.binding import AgentBindingRecord
from local_agent.repository.host_mailbox_reader import (
    _blob_from_snapshot, _git, _json_object, _trusted_origin,
)
from local_agent.repository.host_routing import (
    HostEnrollment, host_record_path, host_status_observation,
    parse_host_status_record,
)

_SHA = re.compile(r"^[0-9a-f]{40}$")
_CONTROL_REF = "refs/heads/agent-control"


def _validate_control_origin(checkout: Path, target: AgentBindingRecord) -> None:
    """Check both configured and Git-rewritten origin before network inspection."""
    _trusted_origin(checkout, target)
    expanded = _git(checkout, "remote", "get-url", "origin", limit=1024).strip()
    repository = target.repository.casefold()
    accepted = {
        f"https://github.com/{repository}", f"https://github.com/{repository}.git",
        f"git@github.com:{repository}", f"git@github.com:{repository}.git",
        f"ssh://git@github.com/{repository}", f"ssh://git@github.com/{repository}.git",
    }
    if expanded.casefold() not in accepted:
        raise ValueError("rewritten origin is not the canonical repository")


def preview_host_status_snapshot(
    control_checkout: Path,
    *,
    host_id: str,
    expected_control_sha: str,
    expected_source_sha: str,
    expected_operator_sha: str,
    enrollment: HostEnrollment,
    target: AgentBindingRecord,
    now: datetime,
    max_age_seconds: int = 120,
) -> dict[str, object]:
    """Read a regular blob from an immutable snapshot and report bounded freshness.

    No network fetch, Git mutation, task claim, heartbeat publication or execution.
    The operator enrollment's provenance must be verified by the calling layer.
    """
    path = host_record_path(host_id, "status.json")
    if any(not isinstance(value, str) or _SHA.fullmatch(value) is None for value in (
        expected_control_sha, expected_source_sha, expected_operator_sha,
    )):
        raise ValueError("host status needs three exact Git SHAs")
    if not isinstance(enrollment, HostEnrollment) or not enrollment.enabled:
        raise ValueError("host has no enabled operator enrollment")
    if enrollment.identity.host_id != host_id:
        raise ValueError("host status is not addressed to the enrolled host")
    if not isinstance(target, AgentBindingRecord):
        raise ValueError("host status target must be a canonical catalog binding")
    if (target.repository_id.casefold(), target.agent_binding) not in {
        (repository_id.casefold(), binding) for repository_id, binding in enrollment.repositories
    }:
        raise ValueError("host enrollment does not contain the target binding")

    checkout = Path(control_checkout)
    _validate_control_origin(checkout, target)

    remote = _git(checkout, "ls-remote", "--heads", "origin", _CONTROL_REF, limit=8192).strip()
    if remote != f"{expected_control_sha}\t{_CONTROL_REF}":
        raise ValueError("host status control HEAD differs from pinned snapshot")
    try:
        commit = _git(checkout, "rev-parse", "--verify", f"{expected_control_sha}^{{commit}}", limit=128).strip()
    except ValueError as exc:
        raise ValueError("pinned host status commit is not cached") from exc
    if commit != expected_control_sha:
        raise ValueError("pinned host status commit is not cached")
    binding = _json_object(_blob_from_snapshot(
        checkout, expected_control_sha, PurePosixPath(".agent/binding.json"), limit=8192,
    ))
    if (binding.get("version") != 1 or binding.get("repository_id") != target.repository_id
            or str(binding.get("repository", "")).casefold() != target.repository.casefold()
            or binding.get("agent_binding") != target.agent_binding):
        raise ValueError("Git control binding does not match canonical target")
    raw = _blob_from_snapshot(checkout, expected_control_sha, path, limit=4096)
    record = parse_host_status_record(raw.encode("utf-8"))
    if (record["host_id"] != enrollment.identity.host_id
            or record["installation_id"] != enrollment.identity.installation_id
            or record["source_revision"] != expected_source_sha):
        raise ValueError("status identity or source revision differs from pinned enrollment")
    observed = host_status_observation(
        record, local_identity=enrollment.identity, now=now,
        max_age_seconds=max_age_seconds,
    )
    # This second remote probe is only a read-side race check, not a lease.
    if _git(checkout, "ls-remote", "--heads", "origin", _CONTROL_REF, limit=8192).strip() != remote:
        raise ValueError("host status control HEAD changed during observation")
    return {
        "status": "validated_observation",
        "source_branch": "agent-control",
        "source_sha": expected_control_sha,
        "operator_registry_sha": expected_operator_sha,
        "host_id": host_id,
        "repository_id": target.repository_id,
        "source_revision": expected_source_sha,
        "observed_state": observed,
        "fresh": observed in ("idle", "busy"),
        "record_state": record["state"],
        "updated_at": record["updated_at"],
        "publisher_authenticated": False,
        "execution_authority": "not_asserted",
        "claimed": False,
        "executed": False,
    }
