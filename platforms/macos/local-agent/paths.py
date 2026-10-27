"""Installed source checkout paths, independent of working directory."""

from pathlib import Path


def repository_root() -> Path:
    """Resolve the checkout containing this package, including worktrees/symlinks."""
    return Path(__file__).resolve().parents[3]
