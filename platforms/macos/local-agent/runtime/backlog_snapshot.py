"""Read-only bounded inspection of local claim/publication residue.

Presence of a claim is not evidence of safe replay. No lease probing,
filesystem repair, cleanup or Git traffic is performed.
"""
from __future__ import annotations

import os
import stat
from pathlib import Path

from local_agent.runtime.health_snapshot import (
    REGISTRY_BYTES, _private_json, _repository_ids,
)

MAX_DIRECTORY_ENTRIES = 256
_KINDS = ("claims", "result-spool", "corrupt-claims")


def _count_records(path: Path) -> int | None:
    """Return None rather than a misleading zero for missing/unsafe paths."""
    if path.is_symlink() or not path.is_dir():
        return None
    try:
        info = path.stat()
        if not stat.S_ISDIR(info.st_mode):
            return None
        if os.name == "posix" and (info.st_uid != os.getuid() or info.st_mode & 0o022):
            return None
        count = 0
        with os.scandir(path) as items:
            for item in items:
                count += 1
                if count > MAX_DIRECTORY_ENTRIES:
                    return None
                if (not item.name.endswith(".json") or item.is_symlink()
                        or not item.is_file(follow_symlinks=False)):
                    return None
        return count
    except (OSError, ValueError):
        return None


def observe_local_backlog(*, state_root: Path, registry_file: Path) -> dict[str, object]:
    """Summarize local records without reading task or claim contents."""
    root = Path(state_root)
    result: dict[str, object] = {
        "schema_version": 1,
        "status": "unverified",
        "repositories": [],
        "totals": {kind: None for kind in _KINDS},
        "warnings": [],
        "execution_authority": "not_asserted",
        "replay_authority": "not_asserted",
        "mutated": False,
    }
    if not root.is_absolute() or root.is_symlink() or not root.is_dir():
        result["warnings"].append("state_root_unavailable")
        return result
    if (root / "repositories").is_symlink():
        result["warnings"].append("repository_state_root_unavailable")
        return result
    try:
        ids = _repository_ids(_private_json(registry_file, REGISTRY_BYTES))
    except (OSError, ValueError, UnicodeError, TypeError):
        result["warnings"].append("registry_unavailable")
        return result

    totals: dict[str, int | None] = {kind: 0 for kind in _KINDS}
    entries: list[dict[str, object]] = []
    for repo_id in ids:
        base = root / "repositories" / repo_id
        counts = {kind: _count_records(base / kind) if not base.is_symlink() else None
                  for kind in _KINDS}
        entries.append({"repository_id": repo_id, "counts": counts})
        for kind, number in counts.items():
            if number is None:
                totals[kind] = None
                result["warnings"].append(f"{kind.replace('-', '_')}_unavailable")
            else:
                previous_total = totals[kind]
                if previous_total is not None:
                    totals[kind] = previous_total + number

    result["repositories"] = entries
    result["totals"] = totals
    result["warnings"] = sorted(set(result["warnings"]))
    result["status"] = "observed" if not result["warnings"] else "unverified"
    return result
