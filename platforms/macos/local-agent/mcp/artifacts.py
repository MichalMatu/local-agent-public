from __future__ import annotations

import base64
import binascii
import hashlib
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path

from local_agent.foundation.process import fsync_directory
from local_agent.mcp.config import MCPServerConfig
from local_agent.mcp.errors import MCPArtifactError, MCPResultTooLargeError

_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._-]+")
_EXTENSIONS = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "audio/wav": ".wav",
    "audio/mpeg": ".mp3",
    "audio/ogg": ".ogg",
    "audio/flac": ".flac",
    "application/pdf": ".pdf",
}


@dataclass(frozen=True)
class PendingArtifact:
    path: Path
    mime_type: str
    size: int
    sha256: str
    data: bytes

    def metadata(self) -> dict[str, object]:
        return {
            "path": str(self.path),
            "mime_type": self.mime_type,
            "size": self.size,
            "sha256": self.sha256,
        }


def _safe_component(value: str) -> str:
    result = _SAFE_NAME_RE.sub("_", value).strip("._-")
    return (result or "tool")[:48]


def prepare_artifact(
    *,
    server: MCPServerConfig,
    artifact_dir: Path,
    tool_name: str,
    index: int,
    mime_type: str | None,
    encoded_data: str,
) -> PendingArtifact:
    if mime_type is None or mime_type not in server.allowed_artifact_mime_types:
        raise MCPArtifactError(
            f"artifact MIME type {mime_type!r} is not allowed for server {server.server_id!r}"
        )
    if not isinstance(encoded_data, str):
        raise MCPArtifactError("artifact data must be base64 text")

    encoded_limit = ((server.max_artifact_bytes + 2) // 3) * 4 + 4
    if len(encoded_data) > encoded_limit:
        raise MCPResultTooLargeError(
            f"artifact base64 exceeds configured {server.max_artifact_bytes}-byte result bound"
        )
    try:
        data = base64.b64decode(encoded_data, validate=True)
    except (binascii.Error, ValueError):
        raise MCPArtifactError("artifact contains invalid base64") from None
    if len(data) > server.max_artifact_bytes:
        raise MCPResultTooLargeError(
            f"artifact exceeds configured {server.max_artifact_bytes}-byte result bound"
        )

    digest = hashlib.sha256(data).hexdigest()
    extension = _EXTENSIONS.get(mime_type, ".bin")
    filename = f"{server.server_id}--{_safe_component(tool_name)}--{index:03d}--{digest}{extension}"
    root = artifact_dir.expanduser().resolve()
    path = root / filename
    return PendingArtifact(
        path=path,
        mime_type=mime_type,
        size=len(data),
        sha256=digest,
        data=data,
    )


def commit_artifact(artifact: PendingArtifact) -> None:
    artifact.path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temp_name = tempfile.mkstemp(
        dir=artifact.path.parent,
        prefix=f".{artifact.path.name}.",
        suffix=".tmp",
    )
    temp = Path(temp_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(artifact.data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, artifact.path)
        fsync_directory(artifact.path.parent)
    except Exception:
        temp.unlink(missing_ok=True)
        raise
