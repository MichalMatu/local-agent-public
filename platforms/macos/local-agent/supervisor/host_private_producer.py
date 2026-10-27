"""Explicit private host Git producer setup; no enrollment, grant or executor.

The profile binds an existing locally pinned operator identity to two existing
transports. A producer is never an authority merely because it can push Git.
"""
from __future__ import annotations

import json
import os
import re
import stat
import subprocess
from pathlib import Path
from typing import Any

from local_agent.operator.remote import (
    _remote_ref, fetch_operator_host_command_grants, fetch_operator_host_registry,
)
from local_agent.repository.host_execution import load_local_operator_pin
from local_agent.paths import repository_root
from local_agent.repository.host_facts import load_local_host_identity
from local_agent.repository.host_routing import HostIdentity
from local_agent.repository.path_safety import has_untrusted_symlink
from local_agent.supervisor.host_control import (
    CONTROL_BRANCH, CONTROL_REPOSITORY, HostControlSource,
    canonical_control_repository,
)
from local_agent.supervisor.host_remote_client import _git, _require_control_origin
from local_agent.supervisor.host_remote_grants import require_exact_host_command_grant

_SHA = re.compile(r"^[0-9a-f]{40}$")
_PROFILE = "producer-profile.json"

def _safe_private_path(path: Path, *, existing: bool = True) -> Path:
    path = Path(path).expanduser()
    if (not path.is_absolute() or path == Path(path.anchor)
            or has_untrusted_symlink(path)):
        raise ValueError("private producer path must be absolute and without symlinks")
    if existing and not path.is_dir():
        raise ValueError("private producer directory does not exist")
    return path


def _require_private_directory(path: Path) -> None:
    if not path.is_dir() or path.is_symlink():
        raise ValueError("private producer parent is not a real directory")
    info = path.stat()
    if os.name == "posix" and (info.st_uid != os.getuid() or info.st_mode & 0o077):
        raise ValueError("private producer directory must belong to this user (0700)")


def _validate_origin(origin_url: str, repository: str = CONTROL_REPOSITORY) -> None:
    selected = canonical_control_repository(repository)
    allowed = {
        prefix + selected + suffix
        for prefix in ("https://github.com/", "git@github.com:", "ssh://git@github.com/")
        for suffix in ("", ".git")
    }
    if not isinstance(origin_url, str) or origin_url.casefold() not in allowed:
        raise ValueError("host producer origin differs from selected GitHub repository")


def _read_authority(
    identity_file: Path, operator_checkout: Path, *,
    expected_operator_sha: str | None = None,
    task_id: str | None = None, task_digest: str | None = None,
) -> tuple[HostIdentity, str]:
    operator_checkout = _safe_private_path(operator_checkout)
    _require_private_directory(operator_checkout.parent)
    if ((operator_checkout / ".git").is_symlink()
            or not (operator_checkout / ".git").is_dir()
            or _git(operator_checkout, "branch", "--show-current").strip() != "operator-control"):
        raise ValueError("operator checkout must be a dedicated operator-control Git checkout")
    source_root = repository_root().resolve()
    operator_root = operator_checkout.resolve()
    if (operator_root == source_root or operator_root in source_root.parents
            or source_root in operator_root.parents):
        raise ValueError("operator checkout overlaps the installed source")
    if _git(operator_checkout, "status", "--porcelain", "--untracked-files=all").strip():
        raise ValueError("operator checkout has unrelated local changes")
    identity = load_local_host_identity(identity_file)
    pin = load_local_operator_pin(identity_file)
    if expected_operator_sha is not None and pin != expected_operator_sha:
        raise ValueError("operator pin changed since private producer setup")
    operator_sha, enrollments = fetch_operator_host_registry(operator_checkout)
    grants_sha, grants = fetch_operator_host_command_grants(operator_checkout)
    if pin != operator_sha or pin != grants_sha or _remote_ref(operator_checkout) != pin:
        raise ValueError("private producer operator authority is unverified")
    if not any(item.identity == identity and item.enabled for item in enrollments):
        raise ValueError("host installation is not enabled in pinned operator enrollment")
    if task_id is not None:
        require_exact_host_command_grant(
            identity=identity, task_id=task_id, task_digest=task_digest or "",
            grants=grants,
        )
    return identity, pin


def _run_setup_git(cwd: Path, *args: str) -> None:
    try:
        result = subprocess.run(
            ["git", "-c", "core.hooksPath=/dev/null", *args],
            cwd=cwd, text=True, capture_output=True, check=False, timeout=60,
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("private producer Git operation timed out") from exc
    if result.returncode != 0:
        raise RuntimeError("private producer Git setup failed; inspect private root")


def setup_private_host_producer(
    *, identity_file: Path, operator_checkout: Path | None, private_root: Path,
    origin_url: str, expected_control_sha: str,
    git_author_name: str, git_author_email: str,
    repository: str = CONTROL_REPOSITORY,
    authorization: str = "operator_grants",
) -> dict[str, str | bool]:
    """Provision a fresh private producer without touching service/owner checkouts.

    Existing Git credentials are used only by Git; nothing is created or stored
    as a credential. An interrupted setup retains its private root for review.
    """
    identity_file = Path(identity_file).expanduser()
    if authorization not in ("operator_grants", "github_writers"):
        raise ValueError("invalid host producer authorization mode")
    if authorization == "operator_grants":
        if operator_checkout is None:
            raise ValueError("legacy producer requires operator checkout")
        operator_checkout = _safe_private_path(operator_checkout)
    elif operator_checkout is not None:
        raise ValueError("GitHub writer producer does not use an operator checkout")
    root = _safe_private_path(private_root, existing=False)
    _require_private_directory(root.parent)
    if root.exists():
        raise FileExistsError("private producer root already exists; refusing overwrite")
    if any((parent / ".git").exists() for parent in (root.parent, *root.parents)):
        raise ValueError("private producer cannot be inside an existing Git checkout")
    repository = canonical_control_repository(repository)
    _validate_origin(origin_url, repository)
    if not isinstance(expected_control_sha, str) or _SHA.fullmatch(expected_control_sha) is None:
        raise ValueError("private producer requires exact control Git SHA")
    for value in (git_author_name, git_author_email):
        if not isinstance(value, str) or not value.strip() or len(value) > 200 or any(
            ord(c) < 32 or ord(c) == 127 for c in value
        ):
            raise ValueError("local Git author metadata is missing or invalid")
    if authorization == "operator_grants":
        identity, pin = _read_authority(identity_file, operator_checkout)
    else:
        identity = load_local_host_identity(identity_file)
        pin = "github_writers"
    # Read-only remote authentication/freshness preflight before making a path.
    probe = subprocess.run(
        ["git", "ls-remote", "--heads", origin_url, f"refs/heads/{CONTROL_BRANCH}"],
        text=True, capture_output=True, timeout=30, check=False,
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
    )
    if probe.returncode or probe.stdout.strip() != (
        f"{expected_control_sha}\trefs/heads/{CONTROL_BRANCH}"
    ):
        raise ValueError("private producer origin authentication or control SHA unverified")
    root.mkdir(mode=0o700)
    # No cleanup of a possibly partially initialized root: fail closed and
    # require explicit operator inspection instead of deleting unknown files.
    checkout = root / "control"
    _run_setup_git(root, "clone", "--quiet", "--no-tags", "--single-branch",
                   "--branch", CONTROL_BRANCH, "--", origin_url, str(checkout))
    _require_control_origin(checkout, repository)
    _validate_origin(_git(checkout, "remote", "get-url", "origin").strip(), repository)
    _run_setup_git(checkout, "config", "--local", "user.name", git_author_name)
    _run_setup_git(checkout, "config", "--local", "user.email", git_author_email)
    head = _git(checkout, "rev-parse", "HEAD").strip()
    remote = _git(checkout, "ls-remote", "--heads", "origin",
                  f"refs/heads/{CONTROL_BRANCH}").strip()
    if head != expected_control_sha or remote != (
        f"{head}\trefs/heads/{CONTROL_BRANCH}"
    ):
        raise ValueError("private producer source advanced during setup; inspect root")
    # Recheck the host identity after clone; legacy grants remain independent.
    if authorization == "operator_grants":
        if _read_authority(identity_file, operator_checkout, expected_operator_sha=pin)[0] != identity:
            raise ValueError("operator identity changed during private producer setup")
    elif load_local_host_identity(identity_file) != identity:
        raise ValueError("host identity changed during private producer setup")
    profile_path = root / _PROFILE
    payload = {
        "version": 1, "host_id": identity.host_id,
        "installation_id": identity.installation_id,
        "identity_file": str(identity_file),
        "operator_checkout": str(operator_checkout) if operator_checkout else "",
        "operator_sha": pin, "control_checkout": str(checkout),
        "origin": origin_url, "repository": repository,
        "authorization": authorization,
    }
    fd = os.open(profile_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    return {
        "status": "configured", "profile": str(profile_path),
        "host_id": identity.host_id, "installation_id": identity.installation_id,
        "control_sha": head, "operator_sha": pin,
        "authorization": authorization,
        "execution_authority": ("exact_digest_grant_required"
                                if authorization == "operator_grants" else "producer_only"),
        "published": False,
    }


def load_private_host_producer(
    profile: Path, *, task_id: str | None = None, task_digest: str | None = None,
    verify_authority: bool = True,
) -> HostControlSource:
    """Revalidate target and origin; legacy operator mode also checks its pin."""
    profile = Path(profile).expanduser()
    if (not profile.is_absolute() or profile.name != _PROFILE
            or has_untrusted_symlink(profile)):
        raise ValueError("private producer profile path is invalid")
    _require_private_directory(profile.parent)
    fd = os.open(profile, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(fd, "rb") as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or info.st_size > 4096:
            raise ValueError("private producer profile is not a private bounded file")
        raw = handle.read(4097)
    def unique_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        data: dict[str, Any] = {}
        for key, value in pairs:
            if key in data:
                raise ValueError("duplicate private producer profile field")
            data[key] = value
        return data
    try:
        record = json.loads(raw.decode("utf-8"), object_pairs_hook=unique_pairs)
    except (ValueError, UnicodeError) as exc:
        raise ValueError("private producer profile is not valid JSON") from exc
    old = {"version", "host_id", "installation_id", "identity_file",
           "operator_checkout", "operator_sha", "control_checkout", "origin"}
    optional = {"repository", "authorization"}
    if (not isinstance(record, dict) or not old <= set(record)
            or set(record) - old - optional
            or type(record["version"]) is not int or record["version"] != 1):
        raise ValueError("private producer profile has invalid schema")
    authorization = record.get("authorization", "operator_grants")
    if authorization not in ("operator_grants", "github_writers") or type(authorization) is not str:
        raise ValueError("unsupported producer authorization mode")
    if authorization == "github_writers":
        if record["operator_checkout"] != "" or record["operator_sha"] != "github_writers":
            raise ValueError("GitHub-writer producer must not carry operator credentials")
    repository = canonical_control_repository(record.get("repository", CONTROL_REPOSITORY))
    _validate_origin(record["origin"], repository)
    source = HostControlSource(
        record["host_id"], record["installation_id"], Path(record["control_checkout"]),
        repository=repository,
    )
    if source.checkout != profile.parent / "control":
        raise ValueError("private producer checkout differs from configured root")
    if authorization == "operator_grants" and _safe_private_path(source.checkout).resolve() == _safe_private_path(
        Path(record["operator_checkout"])
    ).resolve():
        raise ValueError("private producer cannot reuse operator checkout")
    _require_control_origin(source.checkout, repository)
    _validate_origin(_git(source.checkout, "remote", "get-url", "origin").strip(), repository)
    identity = load_local_host_identity(Path(record["identity_file"]))
    if (identity.host_id, identity.installation_id) != (source.host_id, source.installation_id):
        raise ValueError("private producer host identity does not match local file")
    if verify_authority and authorization == "operator_grants":
        _read_authority(
            Path(record["identity_file"]), Path(record["operator_checkout"]),
            expected_operator_sha=record["operator_sha"],
        )
    return source


def sync_private_host_producer(
    profile: Path, *, expected_local_sha: str, expected_remote_sha: str,
) -> dict[str, str | bool]:
    """Explicit exact-SHA fast-forward after a worker/result publication.

    Reuse the producer checkout only if it is clean, still on the pinned
    installation's trusted branch, and is strictly behind the approved remote
    commit. No reset, rebase, force, task replay or implicit push occurs.
    """
    if (_SHA.fullmatch(expected_local_sha or "") is None
            or _SHA.fullmatch(expected_remote_sha or "") is None):
        raise ValueError("producer sync requires exact local and remote Git SHAs")
    source = load_private_host_producer(profile)
    checkout = source.checkout
    if (not (checkout / ".git").is_dir() or (checkout / ".git").is_symlink()
            or _git(checkout, "branch", "--show-current").strip() != CONTROL_BRANCH):
        raise ValueError("producer sync needs the dedicated agent-control branch")
    if _git(checkout, "status", "--porcelain=v1", "--untracked-files=all").strip():
        raise ValueError("producer sync refuses staged, dirty or untracked files")
    local = _git(checkout, "rev-parse", "HEAD").strip()
    ref = f"refs/heads/{CONTROL_BRANCH}"
    remote = _git(checkout, "ls-remote", "--heads", "origin", ref).strip()
    if local != expected_local_sha or remote != f"{expected_remote_sha}\t{ref}":
        raise ValueError("producer sync source differs from pinned Git SHAs")
    if local == expected_remote_sha:
        return {
            "status": "already_current", "control_sha": local,
            "published": False, "execution_authority": "not_asserted",
        }
    # A non-forced fetch rejects rewritten tracking history. We never reset
    # the producer's current branch, and the worker remains the sole executor.
    tracking = f"refs/remotes/origin/{CONTROL_BRANCH}"
    _run_setup_git(
        checkout, "fetch", "--no-tags", "--no-write-fetch-head", "origin",
        f"{ref}:{tracking}",
    )
    fetched = _git(checkout, "rev-parse", tracking).strip()
    if fetched != expected_remote_sha:
        raise ValueError("producer sync fetched a different Git commit")
    _run_setup_git(checkout, "merge-base", "--is-ancestor", local, fetched)
    if (_git(checkout, "status", "--porcelain=v1", "--untracked-files=all").strip()
            or _git(checkout, "rev-parse", "HEAD").strip() != local
            or _git(checkout, "ls-remote", "--heads", "origin", ref).strip() != remote):
        raise ValueError("producer sync checkout or remote changed before fast-forward")
    _run_setup_git(checkout, "merge", "--ff-only", "--no-edit", fetched)
    final = _git(checkout, "rev-parse", "HEAD").strip()
    if (final != expected_remote_sha
            or _git(checkout, "status", "--porcelain=v1", "--untracked-files=all").strip()
            or _git(checkout, "ls-remote", "--heads", "origin", ref).strip() != remote):
        raise RuntimeError("producer sync final Git state is uncertain; inspect checkout")
    return {
        "status": "fast_forwarded", "control_sha": final,
        "published": False, "execution_authority": "not_asserted",
    }
