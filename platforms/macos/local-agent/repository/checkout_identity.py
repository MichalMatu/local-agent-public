"""Pure GitHub remote identity normalization and read-only checkout inspection.

Keep onboarding preflight independent of the mutating repository admin CLI.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

from local_agent.repository.context import RepositoryContext


def normalize_remote_url(value: str) -> str:
    remote = value.strip()
    for prefix in (
        "https://github.com/",
        "http://github.com/",
        "ssh://git@ssh.github.com:443/",
        "ssh://git@ssh.github.com/",
        "ssh://git@github.com/",
        "git@github.com:",
    ):
        if remote.startswith(prefix):
            remote = remote[len(prefix):]
            break
    if remote.endswith(".git"):
        remote = remote[:-4]
    return remote.strip("/")


def require_checkout_origin(
    path: Path, repository: RepositoryContext, label: str,
    *, returncode: int, output: str,
) -> None:
    """Apply the identical remote identity contract for all callers."""
    if returncode != 0:
        raise RuntimeError(f"{label} checkout has no readable origin: {output.strip()}")
    actual = normalize_remote_url(output)
    if actual.casefold() != repository.repository.casefold():
        raise RuntimeError(
            f"{label} checkout origin mismatch: expected {repository.repository}, got {actual}"
        )


def validate_checkout_readonly(path: Path, repository: RepositoryContext, label: str) -> None:
    """A bounded local Git read; never checkout, fetch, reset or write."""
    if not (path / ".git").exists():
        raise RuntimeError(f"{label} checkout missing: {path}")
    result = subprocess.run(
        ["git", "remote", "get-url", "origin"],
        cwd=path, text=True, capture_output=True, timeout=30, check=False,
    )
    require_checkout_origin(
        path, repository, label, returncode=result.returncode, output=result.stdout,
    )
