"""Host-first Git producer staging and independent, immutable result readback.

These reusable operations neither grant commands nor run workers. Only the
existing independently authorized supervisor may execute a staged task.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path, PurePosixPath
from typing import Any

from local_agent.repository.host_mailbox_reader import _blob_from_snapshot, _git, _json_object
from local_agent.repository.host_routing import HostIdentity, reject_host_namespace_symlinks
from local_agent.repository.path_safety import has_untrusted_symlink
from local_agent.runtime.task_contract import task_digest, validate_task
from local_agent.runtime.task_transport import _atomic_write_text_no_clobber
from local_agent.paths import repository_root
from local_agent.supervisor.host_control import (
    CONTROL_BRANCH, CONTROL_REPOSITORY, HostControlSource,
    require_host_publication_identity,
)
from local_agent.supervisor.host_tasks import (
    MAX_LOCAL_REQUEST_BYTES, _TASK_ID_RE, _load_private_request,
    validate_local_host_task,
)

_SHA = re.compile(r"^[0-9a-f]{40}$")
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_RESULTS_DIR = "task-results"


def stage_host_command(
    request: dict[str, Any], *, host_id: str, installation_id: str,
    output_root: Path,
) -> dict[str, str | bool]:
    """Stage a single host-addressed manifest; never commit, push or authorize it.

    The producer works in its own checkout/staging root, never in the service's
    installation. Operator-control grants are deliberately out of scope.
    """
    identity = HostIdentity(host_id, installation_id)
    normalized, _ = validate_local_host_task(request, identity)
    validate_task(normalized, allow_local_codex=True)  # Remote host admission is stricter than local CLI.
    root = Path(output_root)
    if not root.is_absolute() or not root.is_dir() or has_untrusted_symlink(root):
        raise ValueError("host task publication staging root must be a real absolute directory")
    source = HostControlSource(host_id, installation_id, root)
    relative = f"{source.namespace}/tasks/{request['id']}.json"
    reject_host_namespace_symlinks(root, relative)
    manifest = root / relative
    if manifest.is_symlink():
        raise ValueError("host task manifest must not be a symlink")
    encoded = json.dumps(request, sort_keys=True, ensure_ascii=False) + "\n"
    if len(encoded.encode("utf-8")) > MAX_LOCAL_REQUEST_BYTES:
        raise ValueError("remote host task exceeds byte bound")
    _atomic_write_text_no_clobber(manifest, encoded)
    return {
        "status": "staged_only",
        "manifest": str(manifest),
        "task_id": request["id"],
        "task_digest": task_digest(request),
        "published": False,
        "execution_authority": "not_asserted",
    }


def _require_control_origin(checkout: Path, repository: str = CONTROL_REPOSITORY) -> None:
    """Don't present a similarly named unrelated Git repository as host evidence."""
    from local_agent.repository.admin import normalize_remote_url

    origin = _git(checkout, "config", "--get", "remote.origin.url", limit=1024).strip()
    if normalize_remote_url(origin).casefold() != repository.casefold():
        raise ValueError("host result checkout has an untrusted origin")



def _git_write(checkout: Path, *args: str) -> str:
    """Fixed-argv bounded Git write, without checkout-defined hooks or prompts."""
    try:
        run = subprocess.run(
            ["git", "-c", "core.hooksPath=/dev/null", "-c",
             "commit.gpgsign=false", *args],
            cwd=checkout, text=True, capture_output=True, timeout=30,
            check=False, env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("Git publication timed out; inspect remote before any retry") from exc
    if run.returncode or len(run.stdout) > 8192 or len(run.stderr) > 8192:
        raise RuntimeError("Git publication failed or is uncertain; inspect remote and local HEAD")
    return run.stdout.strip()


def publish_staged_host_command(
    source: HostControlSource, *, task_id: str,
    expected_control_sha: str, expected_task_digest: str,
) -> dict[str, str | bool]:
    """Explicit one-task fast-forward producer publication from an isolated checkout.

    Never rebases, force pushes, grants, retries uncertain effects or mutates
    unrelated paths. A failed push can leave a local commit; the caller must
    independently reconcile remote state before attempting further actions.
    """
    HostIdentity(source.host_id, source.installation_id)
    if not isinstance(task_id, str) or _TASK_ID_RE.fullmatch(task_id) is None:
        raise ValueError("invalid host publication task ID")
    if not isinstance(expected_control_sha, str) or _SHA.fullmatch(expected_control_sha) is None:
        raise ValueError("expected control SHA must be an exact Git commit")
    if not isinstance(expected_task_digest, str) or _DIGEST.fullmatch(expected_task_digest) is None:
        raise ValueError("expected host task digest must be SHA-256")
    checkout = Path(source.checkout)
    if (not checkout.is_absolute() or not checkout.is_dir()
            or has_untrusted_symlink(checkout)
            or (checkout / ".git").is_symlink()
            or not (checkout / ".git").is_dir()):
        raise ValueError("host producer must be an isolated non-symlink Git checkout")
    root = checkout.resolve()
    installed = repository_root().resolve()
    if root == installed or root in installed.parents or installed in root.parents:
        raise ValueError("host producer must not be the installed source checkout")
    _require_control_origin(checkout, source.repository)
    if _git(checkout, "branch", "--show-current", limit=128).strip() != CONTROL_BRANCH:
        raise ValueError("host producer is not on agent-control")
    require_host_publication_identity(source)
    head = _git(checkout, "rev-parse", "HEAD", limit=128).strip()
    ref = f"refs/heads/{CONTROL_BRANCH}"
    remote = _git(checkout, "ls-remote", "--heads", "origin", ref, limit=1024).strip()
    if head != expected_control_sha or remote != f"{expected_control_sha}\t{ref}":
        raise ValueError("host producer differs from exact current remote HEAD")

    relative = f"{source.namespace}/tasks/{task_id}.json"
    reject_host_namespace_symlinks(checkout, relative)
    manifest = checkout / relative
    record = _load_private_request(manifest)
    normalized, _ = validate_local_host_task(
        record, HostIdentity(source.host_id, source.installation_id),
    )
    validate_task(normalized, allow_local_codex=True)
    if record["id"] != task_id or task_digest(record) != expected_task_digest:
        raise ValueError("staged host task does not match pinned immutable digest")
    # A tracked or previously published task ID is immutable.
    if _git(checkout, "ls-tree", "HEAD", "--", relative, limit=2048).strip():
        raise ValueError("host task ID exists in the committed control snapshot")
    # Refuse unrelated staged, dirty or untracked files. -z disables quoting.
    status = _git(
        checkout, "status", "--porcelain=v1", "-z", "--untracked-files=all",
        limit=65536,
    )
    if status != f"?? {relative}\0":
        raise ValueError("host producer checkout contains unrelated or non-new changes")
    # Recheck freshness immediately before first Git write; push must still
    # pass Git's ordinary non-force fast-forward admission.
    if _git(checkout, "ls-remote", "--heads", "origin", ref, limit=1024).strip() != remote:
        raise ValueError("host control ref advanced before publication")
    _git_write(checkout, "add", "--", relative)
    _git_write(checkout, "commit", "--quiet", "--message",
               f"Host task: {task_id}", "--", relative)
    commit_sha = _git(checkout, "rev-parse", "HEAD", limit=128).strip()
    if _SHA.fullmatch(commit_sha) is None:
        raise RuntimeError("host producer commit SHA cannot be verified")
    # Explicit one-ref push, with no force, rebasing, auto-fetch or retry.
    _git_write(checkout, "push", "origin", f"HEAD:{ref}")
    confirmed = _git(checkout, "ls-remote", "--heads", "origin", ref, limit=1024).strip()
    if confirmed != f"{commit_sha}\t{ref}":
        raise RuntimeError("host task push result is unverified; inspect remote")
    return {
        "status": "published",
        "task_id": task_id,
        "task_digest": expected_task_digest,
        "control_sha": commit_sha,
        "published": True,
        "execution_authority": "not_asserted",
    }


def read_host_command_result(
    source: HostControlSource, *, task_id: str,
    expected_control_sha: str, expected_task_digest: str,
) -> dict[str, Any]:
    """Read one exact published Git snapshot; no fetch, execution or replay."""
    HostIdentity(source.host_id, source.installation_id)
    if not isinstance(task_id, str) or _TASK_ID_RE.fullmatch(task_id) is None:
        raise ValueError("invalid host result task ID")
    if not isinstance(expected_control_sha, str) or _SHA.fullmatch(expected_control_sha) is None:
        raise ValueError("expected control SHA must be an exact Git commit")
    if not isinstance(expected_task_digest, str) or _DIGEST.fullmatch(expected_task_digest) is None:
        raise ValueError("expected host task digest must be SHA-256")
    checkout = Path(source.checkout)
    if not checkout.is_absolute() or not checkout.is_dir() or has_untrusted_symlink(checkout):
        raise ValueError("host result checkout must be an isolated real directory")
    _require_control_origin(checkout, source.repository)
    branch = _git(checkout, "branch", "--show-current", limit=128).strip()
    if branch != CONTROL_BRANCH:
        raise ValueError("host result checkout has an unexpected branch")
    head = _git(checkout, "rev-parse", "HEAD", limit=128).strip()
    remote = _git(
        checkout, "ls-remote", "--heads", "origin", f"refs/heads/{CONTROL_BRANCH}",
        limit=1024,
    ).strip()
    if head != expected_control_sha or remote != (
        f"{expected_control_sha}\trefs/heads/{CONTROL_BRANCH}"
    ):
        raise ValueError("host result Git head differs from pinned remote snapshot")
    relative = PurePosixPath(
        f"{source.namespace}/{_RESULTS_DIR}/{task_id}.json",
    )
    raw = _blob_from_snapshot(checkout, expected_control_sha, relative,
                              limit=MAX_LOCAL_REQUEST_BYTES)
    record = _json_object(raw)
    if set(record) != {
        "version", "id", "status", "target_host_id",
        "target_installation_id", "task_digest", "result",
    } or type(record["version"]) is not int or record["version"] != 1:
        raise ValueError("published host result has an invalid envelope")
    result = record["result"]
    if (record["id"] != task_id or
            record["target_host_id"] != source.host_id or
            record["target_installation_id"] != source.installation_id or
            record["task_digest"] != expected_task_digest or
            record["status"] not in ("done", "failed", "unknown") or
            not isinstance(result, dict) or
            result.get("id") != task_id or
            result.get("task_digest") != expected_task_digest or
            result.get("status") != record["status"]):
        raise ValueError("host result identity, digest or terminal state mismatch")
    return {
        "status": "verified_snapshot",
        "source_sha": expected_control_sha,
        "task_id": task_id,
        "task_digest": expected_task_digest,
        "result": result,
        "execution_authority": "not_asserted",
    }
