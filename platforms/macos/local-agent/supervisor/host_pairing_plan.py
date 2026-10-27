"""Read-only preflight for an owner-selected GitHub host transport.

This is not pairing, GitHub ACL attestation or authorization. It only verifies
an existing local installation and a clean pinned Git checkout using existing
Core inspectors; the target owner must still confirm actual GitHub writers.
"""
from __future__ import annotations

import re
from pathlib import Path

from local_agent.paths import repository_root
from local_agent.repository.host_facts import load_local_host_identity
from local_agent.repository.host_routing import HostIdentity
from local_agent.supervisor.host_control import CONTROL_BRANCH
from local_agent.supervisor.host_private_producer import (
    _require_private_directory, _safe_private_path,
)
from local_agent.supervisor.host_remote_client import _git

_SHA = re.compile(r"^[0-9a-f]{40}$")
_REPOSITORY = re.compile(r"^[A-Za-z0-9-]{1,39}/[A-Za-z0-9_.-]{1,100}$")
_GITHUB_ORIGIN = re.compile(
    r"^(?:https://github\.com/|git@github\.com:|ssh://git@github\.com/)"
    r"([A-Za-z0-9-]{1,39})/([A-Za-z0-9_.-]{1,100})(?:\.git)?$",
    re.IGNORECASE,
)


def _canonical_repository(value: str) -> str:
    if not isinstance(value, str) or not _REPOSITORY.fullmatch(value):
        raise ValueError("expected GitHub repository must be explicit owner/name")
    owner, name = value.split("/")
    if (owner.startswith("-") or owner.endswith("-") or name in {".", ".."}
            or name.startswith(".") or name.endswith(".")
            or name.endswith(".git")):
        raise ValueError("GitHub repository name is not canonical for pairing")
    return value.casefold()


def _trusted_github_repository(origin: str) -> str:
    if not isinstance(origin, str) or "\n" in origin or "\r" in origin:
        raise ValueError("invalid GitHub origin")
    match = _GITHUB_ORIGIN.fullmatch(origin)
    if match is None:
        raise ValueError("GitHub origin is not an exact supported GitHub.com transport")
    return _canonical_repository(f"{match[1]}/{match[2].removesuffix('.git')}")


def plan_private_github_pairing(
    *, identity_file: Path, control_checkout: Path, repository: str,
    expected_host_id: str, expected_installation_id: str,
    expected_control_sha: str,
) -> dict[str, str | bool]:
    """Fail-closed read-only Git/installation check; NEVER accepts owner pairing.

    No GitHub API writer/ACL or ChatGPT login evidence is available here.
    Therefore a matching repository is transport evidence only, not admission.
    """
    selected = _canonical_repository(repository)
    expected_identity = HostIdentity(expected_host_id, expected_installation_id)
    if not isinstance(expected_control_sha, str) or _SHA.fullmatch(expected_control_sha) is None:
        raise ValueError("pairing plan requires an exact control Git SHA")
    identity_path = Path(identity_file).expanduser()
    identity = load_local_host_identity(identity_path)
    if identity != expected_identity:
        raise ValueError("pairing plan local host installation is different")
    checkout = _safe_private_path(control_checkout)
    _require_private_directory(checkout.parent)
    real_checkout = checkout.resolve()
    real_source = repository_root().resolve()
    real_identity = identity_path.resolve()
    if (real_checkout == real_source or real_checkout in real_source.parents
            or real_source in real_checkout.parents
            or real_identity == real_checkout or real_checkout in real_identity.parents
            or any((parent / ".git").exists()
                   for parent in (identity_path.parent, *identity_path.parents))):
        raise ValueError("pairing plan must not use service source or Git-stored identity")
    if ((checkout / ".git").is_symlink() or not (checkout / ".git").is_dir()):
        raise ValueError("pairing plan needs a dedicated real Git checkout")
    raw = _git(checkout, "config", "--get", "remote.origin.url", limit=1024).strip()
    effective = _git(checkout, "remote", "get-url", "origin", limit=1024).strip()
    if raw.casefold() != effective.casefold():
        raise ValueError("pairing plan rejects rewritten effective Git origins")
    if (_trusted_github_repository(raw) != selected
            or _trusted_github_repository(effective) != selected):
        raise ValueError("pairing plan origin differs from selected private repository")
    if _git(checkout, "branch", "--show-current").strip() != CONTROL_BRANCH:
        raise ValueError("pairing plan checkout must be on agent-control")
    if _git(checkout, "status", "--porcelain=v1", "--untracked-files=all").strip():
        raise ValueError("pairing plan refuses staged, dirty or untracked checkout")
    local = _git(checkout, "rev-parse", "HEAD").strip()
    ref = f"refs/heads/{CONTROL_BRANCH}"
    remote = _git(checkout, "ls-remote", "--heads", "origin", ref).strip()
    if local != expected_control_sha or remote != f"{expected_control_sha}\t{ref}":
        raise ValueError("pairing plan cannot prove exact pinned local/remote HEAD")
    if (_git(checkout, "status", "--porcelain=v1", "--untracked-files=all").strip()
            or _git(checkout, "rev-parse", "HEAD").strip() != local
            or _git(checkout, "ls-remote", "--heads", "origin", ref).strip() != remote):
        raise ValueError("pairing plan checkout changed during inspection")
    return {
        "status": "source_preflight_pass",
        "repository": selected,
        "host_id": identity.host_id,
        "installation_id": identity.installation_id,
        "control_sha": local,
        "paired": False,
        "published": False,
        "github_writers_verified": False,
        "owner_confirmation_required": True,
        "execution_authority": "not_asserted",
    }
