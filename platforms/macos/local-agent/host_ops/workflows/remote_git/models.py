"""Validated inputs for remote Git workspace workflows."""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlsplit

_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_REVISION_PATTERN = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")


def validate_workspace_name(value: str) -> str:
    """Validate one remote Git cache/workspace identity."""
    if not isinstance(value, str) or not _NAME_PATTERN.fullmatch(value):
        raise ValueError("workspace must use 1..64 letters, digits, '.', '_' or '-'")
    return value


@dataclass(frozen=True, slots=True)
class RemoteGitWorkspace:
    repository_url: str
    revision: str
    workspace: str
    lock: str | None = None
    clean_mode: str = "worktree"

    def __post_init__(self) -> None:
        if (
            not isinstance(self.repository_url, str)
            or not self.repository_url
            or self.repository_url.startswith("-")
            or len(self.repository_url) > 2048
            or any(ord(char) < 32 or ord(char) == 127 for char in self.repository_url)
        ):
            raise ValueError("repository_url must be a non-empty literal Git URL/path")
        if self.repository_url != self.repository_url.strip():
            raise ValueError("repository_url must not have surrounding whitespace")
        if "://" in self.repository_url:
            parsed = urlsplit(self.repository_url)
            scheme = parsed.scheme.casefold()
            if parsed.password is not None:
                raise ValueError("repository_url must not embed credentials")
            if scheme in {"http", "https"}:
                if parsed.username is not None:
                    raise ValueError("repository_url must not embed credentials")
                if parsed.query or parsed.fragment:
                    raise ValueError(
                        "HTTP(S) repository_url must not contain query or fragment data"
                    )
        if not isinstance(self.revision, str) or not _REVISION_PATTERN.fullmatch(self.revision):
            raise ValueError("revision must be one full lowercase 40- or 64-hex Git object id")
        validate_workspace_name(self.workspace)
        if self.lock is not None and (
            not isinstance(self.lock, str) or not _NAME_PATTERN.fullmatch(self.lock)
        ):
            raise ValueError("lock must use 1..64 letters, digits, '.', '_' or '-'")
        if self.clean_mode not in {"worktree", "full"}:
            raise ValueError("clean_mode must be 'worktree' or 'full'")
