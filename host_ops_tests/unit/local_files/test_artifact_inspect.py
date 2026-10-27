from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

import local_agent.host_ops.capabilities.local.files.bounds as bounds_module
import local_agent.host_ops.capabilities.local.files.inspect as inspect_module
from local_agent.host_ops.capabilities.local.files import ArtifactInspectionError, LocalArtifactInspector
from local_agent.host_ops.core.execution import ExecutionLimits


def test_inspect_reports_digest_and_metadata(tmp_path: Path) -> None:
    source = tmp_path / "artifact.bin"
    source.write_bytes(b"artifact-data")

    result = LocalArtifactInspector().inspect(source)

    assert result.requested_path == source.absolute()
    assert result.real_path == source.resolve()
    assert result.file_type == "regular_file"
    assert result.size_bytes == len(b"artifact-data")
    assert result.sha256 == hashlib.sha256(b"artifact-data").hexdigest()
    assert result.modified_time_ns == source.stat().st_mtime_ns
    assert result.as_dict()["real_path"] == str(source.resolve())


def test_inspect_rejects_symlink(tmp_path: Path) -> None:
    source = tmp_path / "artifact.bin"
    source.write_bytes(b"data")
    link = tmp_path / "artifact-link.bin"
    link.symlink_to(source)

    with pytest.raises(ArtifactInspectionError, match="must not be a symbolic link"):
        LocalArtifactInspector().inspect(link)


def test_inspect_rejects_path_replacement_after_open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "artifact.bin"
    source.write_bytes(b"original")
    replacement = tmp_path / "replacement.bin"
    replacement.write_bytes(b"replacement")
    original_hash = inspect_module._sha256_fd

    def replace_path(descriptor: int, max_bytes: int, budget) -> str:
        source.unlink()
        source.symlink_to(replacement)
        return original_hash(descriptor, max_bytes, budget)

    monkeypatch.setattr(inspect_module, "_sha256_fd", replace_path)

    with pytest.raises(ArtifactInspectionError, match=r"artifact (changed|became)"):
        LocalArtifactInspector().inspect(source)


def test_inspect_rejects_non_regular_file(tmp_path: Path) -> None:
    directory = tmp_path / "artifact-dir"
    directory.mkdir()

    with pytest.raises(ArtifactInspectionError, match="not a regular file"):
        LocalArtifactInspector().inspect(directory)


def test_inspect_reports_missing_file(tmp_path: Path) -> None:
    missing = tmp_path / "missing.bin"

    with pytest.raises(ArtifactInspectionError, match="could not inspect artifact"):
        LocalArtifactInspector().inspect(missing)


def test_inspect_rejects_artifact_larger_than_explicit_limit(tmp_path: Path) -> None:
    source = tmp_path / "artifact.bin"
    source.write_bytes(b"12345")

    with pytest.raises(ArtifactInspectionError, match="exceeds max_bytes"):
        LocalArtifactInspector().inspect(source, max_bytes=4)


def test_inspect_enforces_one_whole_operation_timeout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "artifact.bin"
    source.write_bytes(b"payload")
    ticks = iter((100.0, 101.0, 106.0))
    monkeypatch.setattr(bounds_module.time, "monotonic", lambda: next(ticks))

    with pytest.raises(ArtifactInspectionError, match="whole-operation timeout"):
        LocalArtifactInspector().inspect(
            source,
            limits=ExecutionLimits(timeout_seconds=5.0),
        )
