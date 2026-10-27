"""Validation for literal remote file paths used by SSH transfer operations."""

from __future__ import annotations

import re
from pathlib import PurePosixPath

_REMOTE_PATH_SEGMENT = re.compile(r"^[A-Za-z0-9._@%+=,-]+$")


def normalize_remote_file_path(remote_path: str) -> str:
    if (
        not isinstance(remote_path, str)
        or not remote_path.startswith("/")
        or remote_path.startswith("//")
    ):
        raise ValueError("remote path must be an absolute POSIX path with a single leading slash")
    if len(remote_path) > 4096 or "\x00" in remote_path:
        raise ValueError("remote path is too long or contains a NUL byte")
    path = PurePosixPath(remote_path)
    if str(path) != remote_path or remote_path == "/":
        raise ValueError("remote path must be normalized and identify a file path")
    for segment in path.parts[1:]:
        if segment in {"", ".", ".."} or not _REMOTE_PATH_SEGMENT.fullmatch(segment):
            raise ValueError("remote path contains unsupported characters or traversal segments")
    return remote_path
