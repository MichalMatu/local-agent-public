"""Offline staging for sanitized per-host facts, not host enrollment or a publisher.

The GitHub control branch remains unchanged. Only a future, separately admitted
Local Agent may publish these observations and its own freshness-aware status.
"""

from __future__ import annotations

import json
import os
import shutil
import uuid
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from local_agent.host_ops.capabilities.local.host.models import HostProfile
from local_agent.host_ops.capabilities.local.tools.models import ToolInspectionResult
from local_agent.repository.host_routing import (
    HostEnrollment,
    HostIdentity,
    canonical_host_id,
    host_record_path,
)
from local_agent.repository.path_safety import has_untrusted_symlink
from local_agent.tool_runtime.host_inventory import preview_host_inventory

MAX_LOCAL_IDENTITY_BYTES = 4096
MAX_HOST_FACT_RECORD_BYTES = 16 * 1024


def initialize_local_host_identity(path: Path, host_id: str) -> HostIdentity:
    """Create a stable local UUID exactly once; enrollment is still required."""
    canonical_host_id(host_id)
    path = Path(path).expanduser()
    if not path.is_absolute() or path == Path(path.anchor):
        raise ValueError("identity file path must be absolute")
    if has_untrusted_symlink(path):
        raise ValueError("identity file path contains a symlink")
    if any((parent / ".git").exists() for parent in (path.parent, *path.parents)):
        raise ValueError("identity file must not be created in a Git working tree")
    if not path.parent.is_dir():
        raise ValueError("identity file parent directory must already exist")
    identity = HostIdentity(host_id=host_id, installation_id=str(uuid.uuid4()))
    encoded = (json.dumps(identity.as_record(), sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
    created = False
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        created = True
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        return identity
    except Exception:
        if created:
            path.unlink(missing_ok=True)
        raise


def load_local_host_identity(path: Path) -> HostIdentity:
    """Read an already configured local identity; never generate or auto-enroll one."""
    path = Path(path).expanduser()
    if (not path.is_absolute() or not path.is_file()
            or has_untrusted_symlink(path)):
        raise ValueError("local host identity must be an absolute regular, non-symlink file")
    if os.name == "posix" and (path.stat().st_mode & 0o077):
        raise ValueError("local host identity must not be accessible by group or others")
    with path.open("rb") as handle:
        raw = handle.read(MAX_LOCAL_IDENTITY_BYTES + 1)
    if not raw or len(raw) > MAX_LOCAL_IDENTITY_BYTES:
        raise ValueError("local host identity exceeds its byte limit")

    def unique_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate local host identity key")
            result[key] = value
        return result

    try:
        record = json.loads(raw.decode("utf-8"), object_pairs_hook=unique_pairs)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("local host identity must be UTF-8 JSON") from exc
    if not isinstance(record, dict) or set(record) != {"version", "host_id", "installation_id"}:
        raise ValueError("local host identity has unknown or missing fields")
    return HostIdentity.from_record(record)


def build_staged_host_facts(
    identity: HostIdentity,
    *,
    enrollments: Sequence[HostEnrollment],
    operator_sha: str,
    source_revision: str,
    collected_at: str,
    profile: HostProfile,
    tools: Sequence[ToolInspectionResult],
) -> dict[str, dict[str, object]]:
    """Create redacted, explicitly non-authoritative facts for one enrolled identity."""
    if not isinstance(identity, HostIdentity):
        raise ValueError("local host identity is invalid")
    if not isinstance(operator_sha, str) or len(operator_sha) != 40 or any(c not in "0123456789abcdef" for c in operator_sha):
        raise ValueError("operator_sha must be a pinned 40-character Git SHA")
    if len(enrollments) > 32 or any(not isinstance(x, HostEnrollment) for x in enrollments):
        raise ValueError("enrollment snapshot is not validated or bounded")
    matching = [x for x in enrollments if x.identity.host_id == identity.host_id]
    if len(matching) != 1 or not matching[0].enabled or matching[0].identity != identity:
        raise ValueError("local installation is not enabled in pinned operator enrollment")
    preview = preview_host_inventory(
        host_id=identity.host_id, source_revision=source_revision,
        collected_at=collected_at, profile=profile, tools=tools,
    )
    marker: dict[str, object] = {
        "record_type": "diagnostic_preview",
        "publication_authority": "not_asserted",
        "operator_registry_sha": operator_sha,
    }
    system = {**preview["system"], **marker, "installation_id": identity.installation_id}
    capabilities = {**preview["capabilities"], **marker, "installation_id": identity.installation_id}
    identity_record = {**identity.as_record(), **marker}
    records = {"identity.json": identity_record, "system.json": system,
               "capabilities.json": capabilities}
    for record in records.values():
        if len(json.dumps(record, sort_keys=True, allow_nan=False).encode("utf-8")) > MAX_HOST_FACT_RECORD_BYTES:
            raise ValueError("host fact record exceeds its byte limit")
    return records


def write_staged_host_facts(
    output_root: Path,
    identity: HostIdentity,
    records: dict[str, dict[str, object]],
    *,
    protected_paths: Sequence[Path] = (),
) -> tuple[Path, ...]:
    """Write only into a newly created offline directory; never write into Git clones."""
    if not isinstance(identity, HostIdentity):
        raise ValueError("host identity is invalid")
    if set(records) != {"identity.json", "system.json", "capabilities.json"}:
        raise ValueError("host fact records must have exactly three supported filenames")
    for name, record in records.items():
        if not isinstance(record, dict) or record.get("host_id") != identity.host_id or record.get("installation_id") != identity.installation_id:
            raise ValueError("staged host facts have a mismatched identity")
        if record.get("publication_authority") != "not_asserted" or record.get("record_type") != "diagnostic_preview":
            raise ValueError("staged host facts cannot assert execution authority")
        host_record_path(identity.host_id, name)

    root = Path(output_root).expanduser()
    if not root.is_absolute() or root == Path(root.anchor):
        raise ValueError("output root must be an isolated absolute directory")
    if has_untrusted_symlink(root):
        raise ValueError("host staging root contains a symlink")
    if any((parent / ".git").exists() for parent in (root, *root.parents)):
        raise ValueError("host staging cannot be inside a Git working tree")
    absolute = root.resolve()
    for protected in protected_paths:
        reserved = Path(protected).expanduser().resolve()
        if absolute == reserved or absolute in reserved.parents or reserved in absolute.parents:
            raise ValueError("host staging overlaps a protected workspace")
    if root.exists():
        raise FileExistsError("host fact output root already exists")
    if not root.parent.is_dir():
        raise ValueError("host staging parent directory must exist")

    target = root / ".agent" / "hosts" / canonical_host_id(identity.host_id)
    created = False
    try:
        # Exclusive mkdir prevents overwriting an existing publication candidate.
        root.mkdir(mode=0o700)
        created = True
        target.mkdir(parents=True, mode=0o700)
        result: list[Path] = []
        for name in ("identity.json", "system.json", "capabilities.json"):
            data = (json.dumps(records[name], sort_keys=True, separators=(",", ":"),
                               ensure_ascii=True, allow_nan=False) + "\n").encode("utf-8")
            if len(data) > MAX_HOST_FACT_RECORD_BYTES:
                raise ValueError("host fact record exceeds its byte limit")
            path = target / name
            with path.open("xb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            result.append(path)
        return tuple(result)
    except Exception:
        # Only the directory created exclusively by this function may be removed.
        if created and root.is_dir() and not root.is_symlink():
            shutil.rmtree(root)
        raise
