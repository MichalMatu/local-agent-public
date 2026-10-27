"""Read-only, local Git project task/result inventory for future CLI and GUI.

A local control commit is a point-in-time observation, NOT remote freshness,
trusted task admission, an execution queue, or permission to replay a claim.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path

from local_agent.repository.context import load_repository_registry
from local_agent.runtime.health_snapshot import REGISTRY_BYTES, _private_json
from local_agent.runtime.task_contract import task_digest

_SHA = re.compile(r"^[0-9a-f]{40}$")
_TASK_NAME = re.compile(r"^[A-Za-z0-9._-]{1,96}\.json$")
_ENTRY_LIMIT = 384
_TREE_BYTES = 65536
_BLOB_BYTES = 32768


def _git(checkout: Path, *args: str, limit: int = _TREE_BYTES) -> bytes:
    """Fixed, bounded local-only Git inspection, without locks or network."""
    environment = os.environ.copy()
    environment.update({
        "GIT_OPTIONAL_LOCKS": "0",
        "GIT_NO_REPLACE_OBJECTS": "1",
        "GIT_TERMINAL_PROMPT": "0",
    })
    result = subprocess.run(
        ["git", "-C", str(checkout), *args], stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL, timeout=8, check=False, env=environment,
    )
    if result.returncode or len(result.stdout) > limit:
        raise ValueError("local_git_evidence_unavailable")
    return result.stdout


def _json_blob(checkout: Path, blob: str) -> dict[str, object]:
    size = _git(checkout, "cat-file", "-s", blob, limit=32).decode("ascii").strip()
    if not size.isdecimal() or not 0 < int(size) <= _BLOB_BYTES:
        raise ValueError("invalid_mailbox_blob_size")
    raw = _git(checkout, "cat-file", "blob", blob, limit=_BLOB_BYTES)
    if len(raw) != int(size):
        raise ValueError("mailbox_blob_size_changed")

    def unique(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate_mailbox_key")
            result[key] = value
        return result

    value = json.loads(raw.decode("utf-8"), object_pairs_hook=unique)
    if not isinstance(value, dict):
        raise ValueError("mailbox_record_not_an_object")
    return value


def _snapshot(checkout: Path, branch: str) -> tuple[str, dict[str, str]]:
    if not checkout.is_absolute() or checkout.is_symlink() or not checkout.is_dir():
        raise ValueError("control_checkout_unavailable")
    if (checkout / ".git").is_symlink() or not (checkout / ".git").is_dir():
        raise ValueError("control_git_directory_unavailable")
    selected = _git(checkout, "symbolic-ref", "-q", "HEAD", limit=256).decode("ascii").strip()
    if selected != f"refs/heads/{branch}":
        raise ValueError("control_branch_mismatch")
    sha = _git(checkout, "rev-parse", "--verify", "HEAD", limit=128).decode("ascii").strip()
    if not _SHA.fullmatch(sha):
        raise ValueError("invalid_control_commit")
    raw = _git(
        checkout, "ls-tree", "-r", "-z", sha, "--",
        ".agent/binding.json", ".agent/tasks", ".agent/results",
    )
    entries = raw.split(b"\0")
    if entries[-1:] != [b""] or len(entries) - 1 > _ENTRY_LIMIT:
        raise ValueError("control_mailbox_inventory_unbounded")
    blobs: dict[str, str] = {}
    for row in entries[:-1]:
        header, sep, path = row.partition(b"\t")
        parts = header.split(b" ")
        if sep != b"\t" or len(parts) != 3 or parts[:2] != [b"100644", b"blob"]:
            raise ValueError("nonregular_control_mailbox_object")
        object_sha = parts[2].decode("ascii")
        if not _SHA.fullmatch(object_sha):
            raise ValueError("invalid_mailbox_blob_identity")
        name = path.decode("utf-8")
        if name in blobs:
            raise ValueError("duplicate_mailbox_object")
        if name == ".agent/binding.json":
            pass
        elif name.startswith((".agent/tasks/", ".agent/results/")):
            suffix = name.rsplit("/", 1)[-1]
            if suffix == ".gitkeep" or (
                name.startswith(".agent/tasks/")
                and ".payload/" in name
            ):
                continue
            if not _TASK_NAME.fullmatch(suffix) or name.count("/") != 2:
                raise ValueError("unexpected_mailbox_path")
        else:
            raise ValueError("unexpected_mailbox_path")
        blobs[name] = object_sha
    return sha, blobs


def observe_local_task_queue(
    *, registry_file: Path, repository_id: str,
) -> dict[str, object]:
    """Count one enabled project's task/result states from its local Git HEAD.

    'awaiting_result' includes possibly claimed/blocked work; no lease, local
    claim, upstream Git ref, producer authority or replay state is checked.
    """
    result: dict[str, object] = {
        "schema_version": 1, "status": "unverified",
        "repository_id": repository_id if isinstance(repository_id, str) and _TASK_NAME.fullmatch(repository_id + ".json") else None,
        "control_commit": None,
        "counts": {"awaiting_result": None, "done": None, "failed": None, "unverified": None},
        "remote_freshness_verified": False,
        "local_claims_observed": False,
        "execution_authority": "not_asserted",
        "replay_authority": "not_asserted",
        "mutated": False,
        "warnings": [],
    }
    try:
        _private_json(registry_file, REGISTRY_BYTES)
        repos = load_repository_registry(path=registry_file, allow_empty=True)
        matches = [repo for repo in repos if repo.repository_id == repository_id]
        if len(matches) != 1 or matches[0].agent_binding is None:
            raise ValueError("repository_selection_unverified")
        repo = matches[0]
        commit, objects = _snapshot(repo.control, repo.control_branch)
        binding_blob = objects.get(".agent/binding.json")
        if binding_blob is None:
            raise ValueError("control_binding_missing")
        identity = _json_blob(repo.control, binding_blob)
        if (identity.get("version") != 1 or identity.get("repository_id") != repo.repository_id
                or identity.get("repository") != repo.repository
                or identity.get("agent_binding") != repo.agent_binding):
            raise ValueError("control_binding_mismatch")
        counts = {"awaiting_result": 0, "done": 0, "failed": 0, "unverified": 0}
        tasks = {
            key.removeprefix(".agent/tasks/"): blob
            for key, blob in objects.items() if key.startswith(".agent/tasks/")
        }
        results = {
            key.removeprefix(".agent/results/"): blob
            for key, blob in objects.items() if key.startswith(".agent/results/")
        }
        for name, blob in tasks.items():
            try:
                manifest = _json_blob(repo.control, blob)
                if manifest.get("id") != name[:-5] or manifest.get("agent_binding") != repo.agent_binding:
                    raise ValueError("task_identity_mismatch")
                if name not in results:
                    counts["awaiting_result"] += 1
                    continue
                published = _json_blob(repo.control, results[name])
                if (published.get("id") != manifest["id"]
                        or published.get("status") not in ("done", "failed")
                        or ("task_digest" in published and published["task_digest"] != task_digest(manifest))):
                    raise ValueError("result_identity_mismatch")
                counts[str(published["status"])] += 1
            except (OSError, ValueError, UnicodeError, json.JSONDecodeError,
                    subprocess.TimeoutExpired):
                counts["unverified"] += 1
        counts["unverified"] += len(results.keys() - tasks.keys())
        result["control_commit"] = commit
        result["counts"] = counts
        result["status"] = "unverified" if counts["unverified"] else "observed"
        if counts["unverified"]:
            result["warnings"].append("mailbox_entries_unverified")
    except (OSError, ValueError, UnicodeError, json.JSONDecodeError,
            subprocess.TimeoutExpired):
        result["warnings"].append("local_control_snapshot_unverified")
    return result
