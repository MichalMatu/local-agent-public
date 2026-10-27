"""Read-only preview of one host-addressed task from an exact Git control commit.

No branch checkout, fetch, claim, queue polling, durable result or task execution.
A missing local commit is UNVERIFIED rather than permission to fetch into a live clone.
Operator enrollment provenance is an explicit caller prerequisite.
"""

from __future__ import annotations

import hashlib
import json
import re
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any, Mapping

from local_agent.daemon.service import load_task_file
from local_agent.foundation import core
from local_agent.repository.binding import AgentBindingRecord
from local_agent.repository.host_routing import (
    HostEnrollment,
    canonical_host_id,
    host_task_path,
    require_host_task_target,
)
from local_agent.runtime.task_contract import (
    MAX_TASK_FILE_BYTES,
    MAX_TASK_PAYLOAD_BYTES,
    MAX_TASK_PAYLOAD_FILES,
    TASK_PAYLOAD_REF_KEY,
    task_digest,
)

_GIT_SHA = re.compile(r"^[0-9a-f]{40}$")
_GIT_TREE_ENTRY = re.compile(r"^(100644) blob ([0-9a-f]{40})\t(.+)$")
_CONTROL_REF = "refs/heads/agent-control"
_MAX_REF_BYTES = 8192


def _git(cwd: Path, *args: str, limit: int = 8192) -> str:
    """Run only fixed argv Git inspections using the registered bounded process helper."""
    result = core.process(["git", *args], cwd, timeout=10, log_commands=False,
                          output_limit=limit + 1024)
    if result["exit_code"] != 0 or result.get("output_truncated"):
        raise ValueError("Git host mailbox inspection failed or exceeded output bounds")
    value = str(result["output"])
    if len(value.encode("utf-8")) > limit:
        raise ValueError("Git host mailbox evidence exceeds its size bound")
    return value


def _trusted_origin(checkout: Path, target: AgentBindingRecord) -> None:
    if not checkout.is_dir() or checkout.is_symlink():
        raise ValueError("host mailbox control checkout must be a real directory")
    origin = _git(checkout, "config", "--get", "remote.origin.url", limit=1024).strip()
    expected = target.repository.casefold()
    accepted = {
        f"https://github.com/{expected}", f"https://github.com/{expected}.git",
        f"git@github.com:{expected}", f"git@github.com:{expected}.git",
        f"ssh://git@github.com/{expected}", f"ssh://git@github.com/{expected}.git",
    }
    if origin.casefold() not in accepted:
        raise ValueError("control origin does not match canonical repository")


def _json_object(raw: str) -> dict[str, Any]:
    def pairs(entries: list[tuple[str, Any]]) -> dict[str, Any]:
        obj: dict[str, Any] = {}
        for name, value in entries:
            if name in obj:
                raise ValueError("duplicate key in host mailbox JSON")
            obj[name] = value
        return obj

    value = json.loads(raw, object_pairs_hook=pairs,
                       parse_constant=lambda _: (_ for _ in ()).throw(ValueError("non-finite JSON value")))
    if not isinstance(value, dict):
        raise ValueError("host mailbox JSON must be an object")
    return value


def _blob_from_snapshot(checkout: Path, sha: str, path: PurePosixPath, *, limit: int) -> str:
    name = path.as_posix()
    entry = _git(checkout, "ls-tree", sha, "--", name, limit=1024).strip()
    matched = _GIT_TREE_ENTRY.fullmatch(entry)
    if matched is None or matched.group(3) != name:
        raise ValueError("host mailbox object is missing or not a regular Git blob")
    blob = matched.group(2)
    size_raw = _git(checkout, "cat-file", "-s", blob, limit=128).strip()
    if not size_raw.isdecimal() or int(size_raw) > limit:
        raise ValueError("host mailbox blob exceeds permitted size")
    value = _git(checkout, "cat-file", "blob", blob, limit=limit)
    encoded = value.encode("utf-8")
    if len(encoded) != int(size_raw) or hashlib.sha1(
        f"blob {size_raw}\0".encode("ascii") + encoded, usedforsecurity=False
    ).hexdigest() != blob:
        raise ValueError("host mailbox Git blob bytes failed UTF-8 round-trip verification")
    return value


def _payload_paths(task: Mapping[str, Any]) -> tuple[str, ...]:
    refs: list[str] = []
    values: list[Any] = [task.get("patch")]
    for field in ("commands", "verify_commands"):
        if isinstance(task.get(field), list):
            values.extend(task[field])
    for field in ("writes", "steps", "verify_steps"):
        if isinstance(task.get(field), list):
            key = "content" if field == "writes" else "command"
            values.extend(item.get(key) for item in task[field] if isinstance(item, dict))
    for value in values:
        if not isinstance(value, dict) or TASK_PAYLOAD_REF_KEY not in value:
            continue
        if set(value) != {TASK_PAYLOAD_REF_KEY}:
            raise ValueError("host mailbox payload reference has extraneous fields")
        relative = value[TASK_PAYLOAD_REF_KEY]
        if not isinstance(relative, str) or len(relative) > 1024:
            raise ValueError("invalid host mailbox payload reference")
        path = PurePosixPath(relative)
        if (not relative or path.is_absolute() or path.as_posix() != relative
                or ".." in path.parts or len(path.parts) < 2
                or path.parts[0] != f"{task['id']}.payload"):
            raise ValueError("host mailbox payload escapes the task's sidecar directory")
        refs.append(relative)
    unique = tuple(sorted(set(refs)))
    if len(unique) > MAX_TASK_PAYLOAD_FILES:
        raise ValueError("host mailbox has too many payload references")
    return unique


def preview_host_mailbox_snapshot(
    control_checkout: Path,
    *,
    host_id: str,
    task_id: str,
    expected_control_sha: str,
    enrollment: HostEnrollment,
    target: AgentBindingRecord,
) -> dict[str, Any]:
    """Validate a Git-hosted task at a pinned current remote HEAD, without admitting it."""
    canonical_host_id(host_id)
    manifest_path = host_task_path(host_id, task_id)
    if not isinstance(expected_control_sha, str) or _GIT_SHA.fullmatch(expected_control_sha) is None:
        raise ValueError("expected_control_sha must be an exact Git commit")
    if not enrollment.enabled or enrollment.identity.host_id != host_id:
        raise ValueError("target host has no enabled operator enrollment")
    checkout = Path(control_checkout)
    _trusted_origin(checkout, target)
    remote = _git(checkout, "ls-remote", "--heads", "origin", _CONTROL_REF,
                  limit=_MAX_REF_BYTES).strip()
    if remote != f"{expected_control_sha}\t{_CONTROL_REF}":
        raise ValueError("agent-control remote HEAD differs from pinned snapshot")
    try:
        actual = _git(checkout, "rev-parse", "--verify", f"{expected_control_sha}^{{commit}}",
                      limit=128).strip()
    except ValueError as exc:
        raise ValueError("pinned agent-control commit is not cached locally") from exc
    if actual != expected_control_sha:
        raise ValueError("pinned agent-control commit is not cached locally")
    binding = _json_object(_blob_from_snapshot(
        checkout, expected_control_sha, PurePosixPath(".agent/binding.json"), limit=8192))
    if (binding.get("version") != 1 or binding.get("repository_id") != target.repository_id
            or str(binding.get("repository", "")).casefold() != target.repository.casefold()
            or binding.get("agent_binding") != target.agent_binding):
        raise ValueError("Git control binding does not match target repository")

    raw = _blob_from_snapshot(checkout, expected_control_sha, manifest_path,
                              limit=MAX_TASK_FILE_BYTES)
    manifest = _json_object(raw)
    if manifest.get("id") != task_id or type(manifest.get("host_mailbox_version")) is not int or manifest["host_mailbox_version"] != 1:
        raise ValueError("host mailbox task id or schema does not match the addressed file")
    require_host_task_target(manifest, local_identity=enrollment.identity,
                             enrollment=enrollment, repository=target)

    # Use the existing task parser/sidecar policy in an ephemeral directory; never
    # read executable payload bytes from the live working tree or print them.
    with tempfile.TemporaryDirectory(prefix="la-host-mailbox-preview-") as folder:
        tasks = Path(folder)
        (tasks / manifest_path.name).write_text(raw, encoding="utf-8")
        total_bytes = 0
        for relative in _payload_paths(manifest):
            payload = _blob_from_snapshot(checkout, expected_control_sha,
                                          manifest_path.parent / relative,
                                          limit=MAX_TASK_PAYLOAD_BYTES)
            total_bytes += len(payload.encode("utf-8"))
            if total_bytes > MAX_TASK_PAYLOAD_BYTES:
                raise ValueError("host mailbox payload set exceeds the total byte bound")
            destination = tasks / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text(payload, encoding="utf-8")
        canonical = load_task_file(tasks / manifest_path.name)
    require_host_task_target(canonical, local_identity=enrollment.identity,
                             enrollment=enrollment, repository=target)
    return {
        "status": "validated_preview",
        "source_branch": "agent-control",
        "source_sha": expected_control_sha,
        "operator_enrollment_host_id": enrollment.identity.host_id,
        "repository_id": target.repository_id,
        "task_id": task_id,
        "task_digest": task_digest(canonical),
        "payload_file_count": len(_payload_paths(manifest)),
        "execution_authority": "not_asserted",
        "claimed": False,
        "executed": False,
    }
