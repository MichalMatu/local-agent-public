"""Stable remote workspace identity derived from a worker-visible repository URL."""

from __future__ import annotations

import hashlib
import re

_WORKSPACE_SAFE = re.compile(r"[^A-Za-z0-9._-]+")


def derive_workspace_name(repository_url: str) -> str:
    """Return a filesystem-safe cache key derived only from repository identity."""
    if not isinstance(repository_url, str) or not repository_url:
        raise ValueError("repository_url must be a non-empty string")

    tail = repository_url.rstrip("/").rsplit("/", 1)[-1].rsplit(":", 1)[-1]
    tail = tail.removesuffix(".git")
    slug = _WORKSPACE_SAFE.sub("-", tail).strip("-._") or "repo"
    digest = hashlib.sha256(repository_url.encode("utf-8")).hexdigest()[:12]
    prefix = slug[: 64 - len(digest) - 1]
    return f"{prefix}-{digest}"
