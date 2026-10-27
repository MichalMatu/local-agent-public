"""Structured results for local artifact operations."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class ArtifactInspectionResult:
    requested_path: Path
    real_path: Path
    file_type: str
    size_bytes: int
    sha256: str
    modified_time_ns: int

    def as_dict(self) -> dict[str, object]:
        return {
            "requested_path": str(self.requested_path),
            "real_path": str(self.real_path),
            "file_type": self.file_type,
            "size_bytes": self.size_bytes,
            "sha256": self.sha256,
            "modified_time_ns": self.modified_time_ns,
        }


@dataclass(frozen=True, slots=True)
class ArtifactDeploymentResult:
    source: Path
    destination: Path
    size_bytes: int
    sha256: str
    replaced_existing: bool
    directory_synced: bool

    def as_dict(self) -> dict[str, object]:
        return {
            "source": str(self.source),
            "destination": str(self.destination),
            "size_bytes": self.size_bytes,
            "sha256": self.sha256,
            "replaced_existing": self.replaced_existing,
            "directory_synced": self.directory_synced,
        }
