"""Validated local Git repository context."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True, slots=True)
class GitRepositoryContext:
    root: Path
    remote_name: str
    remote_url: str
    revision: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "root": str(self.root),
            "remote_name": self.remote_name,
            "revision": self.revision,
        }
