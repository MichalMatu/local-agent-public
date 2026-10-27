"""One-shot, read-only preflight for host fact publication to agent-control.

A preview cannot enroll a host, grant permission or reserve a remote ref. A
future writer must re-check authorization and perform a compare-and-swap push.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from datetime import datetime
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from local_agent.foundation import core
from local_agent.repository.host_facts import MAX_HOST_FACT_RECORD_BYTES
from local_agent.repository.path_safety import has_untrusted_symlink
from local_agent.repository.host_routing import HostEnrollment, HostIdentity, host_record_path
from local_agent.tool_runtime.host_inventory import HOST_INVENTORY_SCHEMA_VERSION
from local_agent.tool_runtime.contract import TOOL_RUNTIME_SCHEMA_VERSION
from local_agent.host_ops.version import JSON_CONTRACT_VERSION

_CONTROL_REF = "refs/heads/agent-control"
_SHA = re.compile(r"^[0-9a-f]{40}$")
_NAMES = ("identity.json", "system.json", "capabilities.json")
_SAFE_TEXT = re.compile(r"^[A-Za-z0-9 ._(),+:~-]{1,128}$")
_TOOL_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,127}$")
_MARKER = {"record_type", "publication_authority", "operator_registry_sha", "installation_id"}
_FIELDS = {
    "identity.json": {"version", "host_id", "installation_id", "record_type",
                      "publication_authority", "operator_registry_sha"},
    "system.json": {"schema_version", "record_type", "host_id", "source_revision",
                    "collected_at", "os", "release", "architecture", "logical_cpu_count",
                    "memory_total_bytes", "root_total_bytes", "root_free_bytes",
                    "gpu_devices"} | _MARKER,
    "capabilities.json": {"schema_version", "record_type", "host_id", "source_revision",
                          "collected_at", "host_ops_json_contract_version",
                          "tool_runtime_schema_version", "tools", "execution_permission"} | _MARKER,
}


def _git(checkout: Path, *args: str, limit: int = 4096) -> str:
    result = core.process(["git", *args], checkout, timeout=10, log_commands=False,
                          output_limit=limit + 1024)
    value = str(result.get("output", ""))
    if result.get("exit_code") != 0 or result.get("output_truncated") or len(value.encode("utf-8")) > limit:
        raise ValueError("host publication Git snapshot is missing or unverified")
    return value


def _read_staged_record(path: Path) -> dict[str, Any]:
    if has_untrusted_symlink(path):
        raise ValueError("staged host facts path contains a symlink")
    if not path.is_file():
        raise ValueError("staged host fact must be a regular file")
    # O_NOFOLLOW protects the final component between preliminary checks and open.
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    with os.fdopen(os.open(path, flags), "rb") as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_HOST_FACT_RECORD_BYTES:
            raise ValueError("staged host fact is not a bounded regular file")
        raw = handle.read(MAX_HOST_FACT_RECORD_BYTES + 1)
    if not raw or len(raw) > MAX_HOST_FACT_RECORD_BYTES:
        raise ValueError("staged host fact exceeds byte limit")

    def unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        obj: dict[str, Any] = {}
        for key, value in pairs:
            if key in obj:
                raise ValueError("duplicate staged host fact key")
            obj[key] = value
        return obj

    try:
        record = json.loads(raw.decode("utf-8"), object_pairs_hook=unique,
                            parse_constant=lambda _: (_ for _ in ()).throw(ValueError("non-finite host fact")))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("staged host fact is not valid UTF-8 JSON") from exc
    if not isinstance(record, dict):
        raise ValueError("staged host fact is not an object")
    return record


def preview_host_facts_publication(
    control_checkout: Path,
    staging_root: Path,
    *,
    identity: HostIdentity,
    enrollments: Sequence[HostEnrollment],
    repository_id: str,
    agent_binding: str,
    expected_operator_sha: str,
    expected_control_sha: str,
) -> dict[str, Any]:
    """Inspect one cached, current control snapshot; never stage, commit, fetch or push.

    Operator provenance, control origin and credential scopes are caller gates.
    Stale remote refs, any existing host subtree and invalid facts fail closed.
    """
    if not isinstance(identity, HostIdentity) or any(
        not isinstance(sha, str) or not _SHA.fullmatch(sha)
        for sha in (expected_operator_sha, expected_control_sha)
    ):
        raise ValueError("publication identity or pinned revision is invalid")
    if len(enrollments) > 32 or any(not isinstance(entry, HostEnrollment) for entry in enrollments):
        raise ValueError("operator enrollment snapshot is invalid")
    matching = [item for item in enrollments if item.identity.host_id == identity.host_id]
    if len(matching) != 1 or not matching[0].enabled or matching[0].identity != identity:
        raise ValueError("installation lacks enabled, matching operator enrollment")
    if (not isinstance(repository_id, str) or not repository_id
            or len(repository_id) > 100 or not re.fullmatch(r"[A-Za-z0-9._-]+", repository_id)
            or not isinstance(agent_binding, str) or not re.fullmatch(
                r"[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}",
                agent_binding,
            )):
        raise ValueError("publication target repository binding is invalid")
    if (repository_id.casefold(), agent_binding) not in {
        (repo.casefold(), binding) for repo, binding in matching[0].repositories
    }:
        raise ValueError("operator enrollment does not authorize this repository binding")
    root = Path(staging_root).expanduser()
    if not root.is_absolute() or not root.is_dir() or has_untrusted_symlink(root):
        raise ValueError("staged host facts root must be a non-symlink absolute directory")
    host_dir = root / ".agent" / "hosts" / identity.host_id
    if not host_dir.is_dir() or host_dir.is_symlink() or set(p.name for p in host_dir.iterdir()) != set(_NAMES):
        raise ValueError("staged host facts contain missing or unexpected records")
    records: dict[str, dict[str, Any]] = {}
    for name in _NAMES:
        record = _read_staged_record(host_dir / name)
        if set(record) != _FIELDS[name]:
            raise ValueError("staged host fact contains missing or unsupported fields")
        if (record.get("host_id") != identity.host_id
                or record.get("installation_id") != identity.installation_id
                or record.get("publication_authority") != "not_asserted"
                or record.get("record_type") != "diagnostic_preview"
                or record.get("operator_registry_sha") != expected_operator_sha):
            raise ValueError("staged host fact is inconsistent with enrolled identity")
        records[name] = record
    if records["identity.json"] != {
        **identity.as_record(), "record_type": "diagnostic_preview",
        "publication_authority": "not_asserted", "operator_registry_sha": expected_operator_sha,
    }:
        raise ValueError("staged identity record is inconsistent")
    for name in ("system.json", "capabilities.json"):
        record = records[name]
        if type(record.get("schema_version")) is not int or record["schema_version"] != HOST_INVENTORY_SCHEMA_VERSION:
            raise ValueError("unsupported staged host facts schema")
        if not isinstance(record.get("source_revision"), str) or not _SHA.fullmatch(record["source_revision"]):
            raise ValueError("staged host facts lack an exact source revision")
        timestamp = record.get("collected_at")
        if not isinstance(timestamp, str) or len(timestamp) > 64:
            raise ValueError("staged host facts have invalid collection time")
        try:
            observed = datetime.fromisoformat(timestamp)
        except ValueError as exc:
            raise ValueError("staged host facts have invalid collection time") from exc
        if observed.tzinfo is None or observed.utcoffset() is None:
            raise ValueError("staged host facts require a timezone-aware collection time")
    if any(records["system.json"][field] != records["capabilities.json"][field]
           for field in ("source_revision", "collected_at")):
        raise ValueError("staged host facts must come from one observation")
    if records["capabilities.json"].get("execution_permission") != "not_asserted":
        raise ValueError("staged capabilities cannot grant execution")
    system = records["system.json"]
    for field in ("os", "release", "architecture"):
        if not isinstance(system[field], str) or not _SAFE_TEXT.fullmatch(system[field]):
            raise ValueError("staged host platform text is not sanitized")
    for field in ("logical_cpu_count", "memory_total_bytes", "root_total_bytes", "root_free_bytes"):
        count = system[field]
        if count is not None and (type(count) is not int or count < 0):
            raise ValueError("staged capacity field is invalid")
    gpus = system["gpu_devices"]
    if gpus is not None and (not isinstance(gpus, list) or len(gpus) > 16 or any(
        not isinstance(name, str) or not _SAFE_TEXT.fullmatch(name) for name in gpus
    )):
        raise ValueError("staged GPU observations are invalid")
    caps = records["capabilities.json"]
    if (type(caps["host_ops_json_contract_version"]) is not int
            or caps["host_ops_json_contract_version"] != JSON_CONTRACT_VERSION
            or type(caps["tool_runtime_schema_version"]) is not int
            or caps["tool_runtime_schema_version"] != TOOL_RUNTIME_SCHEMA_VERSION):
        raise ValueError("unsupported staged capabilities contract")
    tools = caps.get("tools")
    if not isinstance(tools, list) or len(tools) > 128:
        raise ValueError("staged tool observations must be a bounded list")
    names: list[str] = []
    for tool in tools:
        if not isinstance(tool, dict) or set(tool) != {"name", "present", "version_probe_ok", "version"}:
            raise ValueError("staged tool observation has unknown fields")
        name = tool["name"]
        if not isinstance(name, str) or not _TOOL_NAME.fullmatch(name):
            raise ValueError("staged tool name is invalid")
        if type(tool["present"]) is not bool or type(tool["version_probe_ok"]) is not bool:
            raise ValueError("staged tool readiness flags must be booleans")
        version = tool["version"]
        if version is not None and (not isinstance(version, str) or not _SAFE_TEXT.fullmatch(version)):
            raise ValueError("staged tool version is not sanitized")
        if tool["version_probe_ok"] and (not tool["present"] or version is None):
            raise ValueError("staged tool cannot assert readiness without a successful probe")
        if not tool["version_probe_ok"] and version is not None:
            raise ValueError("staged tool version cannot imply readiness after probe failure")
        names.append(name)
    if names != sorted(set(names)):
        raise ValueError("staged tool observations are duplicate or unsorted")

    checkout = Path(control_checkout)
    if not checkout.is_dir() or checkout.is_symlink():
        raise ValueError("control checkout must be a real directory")
    # Fixed argv; no shell, network fetch, repository mutation or checkout.
    remote = _git(checkout, "ls-remote", "--heads", "origin", _CONTROL_REF).strip()
    if remote != f"{expected_control_sha}\t{_CONTROL_REF}":
        raise ValueError("control branch changed since expected SHA (CAS conflict)")
    commit = _git(checkout, "rev-parse", "--verify", f"{expected_control_sha}^{{commit}}", limit=128).strip()
    if commit != expected_control_sha:
        raise ValueError("pinned control commit is not cached locally")
    subtree = f".agent/hosts/{identity.host_id}"
    if _git(checkout, "ls-tree", "-r", "--name-only", expected_control_sha, "--", subtree).strip():
        raise ValueError("host namespace already exists at the control snapshot")
    # Recheck remote after local inspection; this remains a preview, not a lease.
    if _git(checkout, "ls-remote", "--heads", "origin", _CONTROL_REF).strip() != remote:
        raise ValueError("control branch changed during publication preview")
    digests = {
        host_record_path(identity.host_id, name).as_posix(): hashlib.sha256(
            (json.dumps(records[name], sort_keys=True, separators=(",", ":"),
                        ensure_ascii=True, allow_nan=False) + "\n").encode("utf-8")
        ).hexdigest() for name in _NAMES
    }
    return {
        "status": "publication_preview_only", "source_branch": "agent-control",
        "expected_control_sha": expected_control_sha,
        "operator_registry_sha": expected_operator_sha,
        "host_id": identity.host_id,
        "repository_id": repository_id,
        "agent_binding": agent_binding,
        "source_revision": records["system.json"]["source_revision"],
        "file_sha256": digests,
        "requires_write_time_cas": True, "published": False,
        "heartbeat_published": False, "execution_authority": "not_asserted",
    }
