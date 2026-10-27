"""Bounded, non-authoritative host inventory preview for future GitHub host records.

Detected executables and supplied host ids are observations, not enrollment or execution grants.
This module performs no I/O, task routing, repository admission or device effects.
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from datetime import datetime

from local_agent.host_ops.capabilities.local.host.models import HostProfile
from local_agent.host_ops.capabilities.local.tools.models import ToolInspectionResult
from local_agent.host_ops.version import JSON_CONTRACT_VERSION

from .contract import TOOL_RUNTIME_SCHEMA_VERSION

HOST_INVENTORY_SCHEMA_VERSION = 1
_HOST_ID_RE = re.compile(r"^[a-z][a-z0-9-]{2,63}$")
_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_SAFE_METADATA = re.compile(r"^[A-Za-z0-9 ._(),+:~-]{1,128}$")
_TOOL_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,127}$")
_MAX_MANIFEST_BYTES = 16 * 1024


def _bounded_public_text(value: str | None) -> str | None:
    if isinstance(value, str) and _SAFE_METADATA.fullmatch(value):
        return value
    return None


def _check_size(payload: dict[str, object]) -> dict[str, object]:
    if len(json.dumps(payload, sort_keys=True, ensure_ascii=True).encode("utf-8")) > _MAX_MANIFEST_BYTES:
        raise ValueError("host inventory exceeds the bounded manifest size")
    return payload


def preview_host_inventory(
    *,
    host_id: str,
    source_revision: str,
    collected_at: str,
    profile: HostProfile,
    tools: Sequence[ToolInspectionResult],
) -> dict[str, dict[str, object]]:
    """Build publication-shaped previews, without vouching for host enrollment/policy."""
    if not isinstance(host_id, str) or _HOST_ID_RE.fullmatch(host_id) is None:
        raise ValueError("host_id must be a stable canonical identifier")
    if not isinstance(source_revision, str) or _SHA_RE.fullmatch(source_revision) is None:
        raise ValueError("source_revision must be an exact 40-character git SHA")
    try:
        observed = datetime.fromisoformat(collected_at)
    except (TypeError, ValueError) as exc:
        raise ValueError("collected_at must be a timezone-aware timestamp") from exc
    if observed.tzinfo is None or observed.utcoffset() is None:
        raise ValueError("collected_at must be a timezone-aware timestamp")
    if len(tools) > 64:
        raise ValueError("at most 64 executable inspections may be reported")

    os_name = _bounded_public_text(profile.system)
    release = _bounded_public_text(profile.release)
    architecture = _bounded_public_text(profile.architecture)
    if not all((os_name, release, architecture)):
        raise ValueError("host platform identification cannot be safely published")

    def nonnegative(value: int | None) -> int | None:
        if value is None:
            return None
        if type(value) is not int or value < 0:
            raise ValueError("invalid host capacity value")
        return value

    gpu = None
    if profile.gpu_devices is not None:
        gpu = [name for raw in profile.gpu_devices if (name := _bounded_public_text(raw))]
        gpu = gpu[:16]
    system = _check_size({
        "schema_version": HOST_INVENTORY_SCHEMA_VERSION,
        "record_type": "diagnostic_preview",
        "host_id": host_id,
        "source_revision": source_revision,
        "collected_at": collected_at,
        "os": os_name,
        "release": release,
        "architecture": architecture,
        "logical_cpu_count": nonnegative(profile.logical_cpu_count),
        "memory_total_bytes": nonnegative(profile.memory_total_bytes),
        "root_total_bytes": nonnegative(profile.root_total_bytes),
        "root_free_bytes": nonnegative(profile.root_free_bytes),
        "gpu_devices": gpu,
    })
    entries: list[dict[str, object]] = []
    seen: set[str] = set()
    for tool in tools:
        if (
            not isinstance(tool, ToolInspectionResult)
            or not isinstance(tool.name, str)
            or _TOOL_NAME.fullmatch(tool.name) is None
            or type(tool.present) is not bool
        ):
            raise ValueError("invalid executable inspection record")
        if tool.name in seen:
            raise ValueError("duplicate executable inspection record")
        seen.add(tool.name)
        version = _bounded_public_text(tool.version)
        ready = bool(tool.present and tool.error is None and tool.version_exit_code == 0 and version)
        entries.append({
            "name": tool.name,
            "present": bool(tool.present),
            "version_probe_ok": ready,
            "version": version if ready else None,
        })
    capabilities = _check_size({
        "schema_version": HOST_INVENTORY_SCHEMA_VERSION,
        "record_type": "diagnostic_preview",
        "host_id": host_id,
        "source_revision": source_revision,
        "collected_at": collected_at,
        "host_ops_json_contract_version": JSON_CONTRACT_VERSION,
        "tool_runtime_schema_version": TOOL_RUNTIME_SCHEMA_VERSION,
        "tools": sorted(entries, key=lambda item: str(item["name"])),
        "execution_permission": "not_asserted",
    })
    return {"system": system, "capabilities": capabilities}
