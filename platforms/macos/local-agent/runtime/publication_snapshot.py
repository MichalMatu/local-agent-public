"""Bounded, read-only census of durable result publication spools.

Spool files are retention evidence, never proof of safe replay,
successful execution or eligibility for network publication.
"""
from __future__ import annotations

from pathlib import Path

from local_agent.runtime.backlog_snapshot import _count_records, observe_local_backlog


def observe_local_publications(
    *, state_root: Path, registry_file: Path,
) -> dict[str, object]:
    """Count installation and default-project result-spool files only.

    This deliberately does not inspect project host-mailbox namespace
    subtrees; unobserved locations must not be reported as empty.
    """
    backlog = observe_local_backlog(
        state_root=state_root, registry_file=registry_file,
    )
    root = Path(state_root)
    host_count = None
    if root.is_absolute() and root.is_dir() and not root.is_symlink():
        host_count = _count_records(root / "result-spool")

    projects = [
        {"repository_id": item["repository_id"],
         "durable_spool_files": item["counts"]["result-spool"]}
        for item in backlog["repositories"]
    ]
    counts = [host_count, *(item["durable_spool_files"] for item in projects)]
    registry_known = "registry_unavailable" not in backlog["warnings"]
    total = (sum(counts) if registry_known and all(isinstance(n, int) for n in counts)
             else None)
    warnings = [
        *(("installation_spool_unavailable",) if host_count is None else ()),
        *(f"backlog:{warning}" for warning in backlog["warnings"]),
    ]
    return {
        "schema_version": 1,
        "status": "observed" if not warnings else "unverified",
        "installation_spool_files": host_count,
        "repositories": projects,
        "total_spool_files": total,
        "additional_host_mailbox_namespaces_observed": False,
        "warnings": sorted(set(warnings)),
        "source": "local_files",
        "consistent_point_in_time": False,
        "publication_eligibility": "not_asserted",
        "execution_authority": "not_asserted",
        "replay_authority": "not_asserted",
        "mutated": False,
    }
