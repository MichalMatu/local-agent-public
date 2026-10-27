"""Offline-only host status CAS fixture; never a production publisher.

This exercises the exact one-path Git commit / compare-and-swap contract on a
caller-owned, explicitly marked disposable local bare repository. There is no
GitHub transport, credential provider, daemon integration or enrollment writer.
"""

from __future__ import annotations

import json
import re
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any

from local_agent.foundation import core
from local_agent.repository.path_safety import has_untrusted_symlink
from local_agent.repository.host_routing import (
    HostEnrollment, HostIdentity, host_record_path, parse_host_status_record,
)

_CONTROL_REF = "refs/heads/agent-control"
_SHA = re.compile(r"^[0-9a-f]{40}$")
_MARKER = ".local-agent-disposable-cas-fixture"


def _git(cwd: Path, *args: str, max_bytes: int = 4096) -> str:
    result = core.process(["git", *args], cwd, timeout=15, log_commands=False,
                          output_limit=max_bytes + 1024)
    output = str(result.get("output", ""))
    if (result.get("exit_code") != 0 or result.get("output_truncated")
            or len(output.encode("utf-8")) > max_bytes):
        raise ValueError("offline Git CAS operation failed or was unverified")
    return output.strip()


def _require_fixture_root(root: Path, bare: Path) -> tuple[Path, Path]:
    # macOS uses /var -> /private/var for its normal temporary directory.
    # Resolve ancestor aliases only after rejecting symlinks owned by the
    # fixture itself, so subsequent Git paths have one canonical identity.
    if (not root.is_absolute() or not root.is_dir() or has_untrusted_symlink(root)
            or not (root / _MARKER).is_file() or (root / _MARKER).is_symlink()
            or (root / _MARKER).read_bytes() != b"offline-fixture-only\n"):
        raise ValueError("offline CAS fixture root must carry the explicit marker")
    try:
        relative_bare = bare.relative_to(root)
    except ValueError as exc:
        raise ValueError("offline CAS remote must be a bare directory within the fixture") from exc
    canonical_root = root.resolve(strict=True)
    canonical_bare = bare.resolve(strict=True)
    if (not bare.is_absolute() or not relative_bare.parts or ".." in relative_bare.parts
            or not bare.is_dir() or has_untrusted_symlink(bare) or bare.suffix != ".git"
            or any((root.joinpath(*relative_bare.parts[:index])).is_symlink()
                   for index in range(1, len(relative_bare.parts) + 1))
            or canonical_root not in canonical_bare.parents):
        raise ValueError("offline CAS remote must be a bare directory within the fixture")
    if _git(canonical_bare, "rev-parse", "--is-bare-repository") != "true":
        raise ValueError("offline CAS remote is not bare")
    return canonical_root, canonical_bare


def simulate_host_status_cas(
    fixture_root: Path, bare_remote: Path, *, expected_control_sha: str,
    expected_operator_sha: str, expected_source_sha: str, local_identity: HostIdentity,
    enrollment: HostEnrollment, repository_id: str, agent_binding: str,
    status_bytes: bytes,
) -> dict[str, Any]:
    """One-off CAS **simulation** against a marked disposable local bare repo.

    A rejected/unknown push is never replayed. This does not authenticate a
    host, enroll an installation or assert genuine daemon heartbeat liveness.
    """
    if (not isinstance(local_identity, HostIdentity)
            or not isinstance(enrollment, HostEnrollment)
            or not enrollment.enabled or enrollment.identity != local_identity
            or not isinstance(repository_id, str) or not 1 <= len(repository_id) <= 100
            or re.fullmatch(r"[A-Za-z0-9._-]+", repository_id) is None
            or not isinstance(agent_binding, str)
            or not any(repo.casefold() == repository_id.casefold() and bind == agent_binding
                       for repo, bind in enrollment.repositories)
            or not isinstance(expected_control_sha, str) or not _SHA.fullmatch(expected_control_sha)
            or not isinstance(expected_operator_sha, str) or not _SHA.fullmatch(expected_operator_sha)
            or not isinstance(expected_source_sha, str) or not _SHA.fullmatch(expected_source_sha)):
        raise ValueError("status fixture requires exact identity, enrollment and revisions")
    status = parse_host_status_record(status_bytes)
    if (status["host_id"] != local_identity.host_id
            or status["installation_id"] != local_identity.installation_id):
        raise ValueError("status fixture does not match the operator enrollment")
    if status["source_revision"] != expected_source_sha:
        raise ValueError("status fixture source revision is not independently pinned")
    root, bare = _require_fixture_root(Path(fixture_root), Path(bare_remote))
    if _git(bare, "rev-parse", "--verify", _CONTROL_REF) != expected_control_sha:
        raise ValueError("offline CAS expected SHA mismatch")
    path = host_record_path(local_identity.host_id, "status.json").as_posix()
    identity_path = host_record_path(local_identity.host_id, "identity.json").as_posix()
    with tempfile.TemporaryDirectory(prefix="host-status-cas-", dir=root) as directory:
        work = Path(directory) / "working"
        _git(root, "clone", "--no-local", "--quiet", str(bare), str(work))
        if _git(work, "rev-parse", "HEAD") != expected_control_sha:
            raise ValueError("offline CAS fixture checkout changed")
        try:
            original = _git(work, "show", f"{expected_control_sha}:{identity_path}", max_bytes=2048)
            pinned_identity = json.loads(original)
        except (ValueError, json.JSONDecodeError) as exc:
            raise ValueError("offline CAS requires existing host identity facts") from exc
        if (not isinstance(pinned_identity, dict)
                or pinned_identity.get("host_id") != local_identity.host_id
                or pinned_identity.get("installation_id") != local_identity.installation_id
                or pinned_identity.get("operator_registry_sha") != expected_operator_sha
                or pinned_identity.get("publication_authority") != "not_asserted"
                or pinned_identity.get("record_type") != "diagnostic_preview"):
            raise ValueError("offline CAS host identity facts do not match pinned enrollment")
        target = work / path
        if target.is_symlink() or any(p.is_symlink() for p in target.parents):
            raise ValueError("offline CAS status target contains a symlink")
        if target.exists():
            prior = parse_host_status_record(target.read_bytes())
            if (prior["host_id"] != local_identity.host_id
                    or prior["installation_id"] != local_identity.installation_id
                    or datetime.fromisoformat(status["updated_at"]) <= datetime.fromisoformat(prior["updated_at"])):
                raise ValueError("offline CAS status must advance the matching heartbeat")
        target.parent.mkdir(parents=True, exist_ok=True)
        encoded = (json.dumps(status, sort_keys=True, separators=(",", ":"),
                              ensure_ascii=True, allow_nan=False) + "\n").encode("utf-8")
        target.write_bytes(encoded)
        _git(work, "add", "--", path)
        if _git(work, "diff", "--cached", "--name-only") != path:
            raise ValueError("offline CAS stage changed unexpected paths")
        _git(work, "-c", "user.name=Offline CAS Fixture", "-c",
             "user.email=offline@example.invalid", "commit", "--quiet", "-m",
             "test: offline host status CAS fixture", "--", path)
        commit_sha = _git(work, "rev-parse", "HEAD")
        if not _SHA.fullmatch(commit_sha):
            raise ValueError("offline CAS commit is invalid")
        if _git(bare, "rev-parse", _CONTROL_REF) != expected_control_sha:
            raise ValueError("offline CAS concurrent writer advanced the ref")
        # A force-with-lease parameter is a compare-and-swap constraint; the
        # new commit is a normal fast-forward descendant, never force-written.
        try:
            _git(work, "push", "--porcelain", f"--force-with-lease={_CONTROL_REF}:{expected_control_sha}",
                 "origin", f"HEAD:{_CONTROL_REF}")
        except ValueError as exc:
            raise ValueError("offline CAS failed; effect unknown, never auto-retry") from exc
        confirmed = _git(bare, "rev-parse", _CONTROL_REF)
        if confirmed != commit_sha:
            raise ValueError("offline CAS acknowledgement unknown; never auto-retry")
    return {
        "status": "offline_fixture_only", "published_to_production": False,
        "enrollment_created": False, "daemon_heartbeat_asserted": False,
        "execution_authority": "not_asserted", "previous_sha": expected_control_sha,
        "fixture_commit_sha": commit_sha, "changed_path": path,
    }
