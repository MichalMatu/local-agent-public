from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

import local_agent.host_ops.capabilities.local.files.bounds as bounds_module
import local_agent.host_ops.capabilities.local.files.deploy as deploy_module
from local_agent.host_ops.capabilities.local.files import ArtifactDeploymentError, LocalArtifactDeployer
from local_agent.host_ops.core.execution import ExecutionLimits


def test_deploy_copies_and_verifies_regular_file(tmp_path: Path) -> None:
    source = tmp_path / "source.bin"
    source.write_bytes(b"firmware-data" * 1024)
    target = tmp_path / "target"
    target.mkdir()

    result = LocalArtifactDeployer().deploy(source, target, destination_name="firmware.bin")

    destination = target / "firmware.bin"
    assert destination.read_bytes() == source.read_bytes()
    assert result.destination == destination.resolve()
    assert result.size_bytes == source.stat().st_size
    assert result.sha256 == hashlib.sha256(source.read_bytes()).hexdigest()
    assert result.replaced_existing is False
    assert result.as_dict()["destination"] == str(destination.resolve())
    assert not list(target.glob(".firmware.bin.hostops-*"))


def test_deploy_requires_explicit_replace(tmp_path: Path) -> None:
    source = tmp_path / "source.bin"
    source.write_bytes(b"new")
    target = tmp_path / "target"
    target.mkdir()
    destination = target / "firmware.bin"
    destination.write_bytes(b"old")

    with pytest.raises(ArtifactDeploymentError, match="explicit replace intent"):
        LocalArtifactDeployer().deploy(source, target, destination_name="firmware.bin")

    result = LocalArtifactDeployer().deploy(
        source,
        target,
        destination_name="firmware.bin",
        replace=True,
    )
    assert destination.read_bytes() == b"new"
    assert result.replaced_existing is True


def test_deploy_no_clobber_refuses_destination_that_appears_during_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source.bin"
    source.write_bytes(b"new")
    target = tmp_path / "target"
    target.mkdir()
    destination = target / "firmware.bin"
    original_commit = deploy_module._commit_no_clobber

    def race_commit(temporary_path: Path, commit_destination: Path) -> None:
        commit_destination.write_bytes(b"concurrent")
        original_commit(temporary_path, commit_destination)

    monkeypatch.setattr(deploy_module, "_commit_no_clobber", race_commit)

    with pytest.raises(ArtifactDeploymentError, match="appeared during deployment"):
        LocalArtifactDeployer().deploy(source, target, destination_name="firmware.bin")

    assert destination.read_bytes() == b"concurrent"
    assert not list(target.glob(".firmware.bin.hostops-*"))


def test_deploy_cleans_own_reservation_when_commit_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source.bin"
    source.write_bytes(b"new")
    target = tmp_path / "target"
    target.mkdir()
    destination = target / "firmware.bin"

    def fail_replace(_source: Path, _destination: Path) -> None:
        raise OSError("simulated commit failure")

    monkeypatch.setattr(deploy_module.os, "replace", fail_replace)

    with pytest.raises(ArtifactDeploymentError, match="failed before commit"):
        LocalArtifactDeployer().deploy(source, target, destination_name="firmware.bin")

    assert not destination.exists()
    assert not list(target.glob(".firmware.bin.hostops-*"))


@pytest.mark.parametrize(
    "name",
    ["../firmware.bin", "dir/firmware.bin", "dir\\firmware.bin", ".", ""],
)
def test_deploy_rejects_non_filename_destination(tmp_path: Path, name: str) -> None:
    source = tmp_path / "source.bin"
    source.write_bytes(b"data")
    target = tmp_path / "target"
    target.mkdir()

    with pytest.raises(ArtifactDeploymentError, match="one plain filename"):
        LocalArtifactDeployer().deploy(source, target, destination_name=name)


def test_deploy_rejects_source_and_destination_symlinks(tmp_path: Path) -> None:
    source = tmp_path / "source.bin"
    source.write_bytes(b"data")
    source_link = tmp_path / "source-link.bin"
    source_link.symlink_to(source)
    target = tmp_path / "target"
    target.mkdir()

    with pytest.raises(ArtifactDeploymentError, match="source must not be a symbolic link"):
        LocalArtifactDeployer().deploy(source_link, target)

    target_link = tmp_path / "target-link"
    target_link.symlink_to(target, target_is_directory=True)
    with pytest.raises(ArtifactDeploymentError, match="destination directory must not"):
        LocalArtifactDeployer().deploy(source, target_link)


def test_deploy_rejects_directory_source_and_non_directory_target(tmp_path: Path) -> None:
    source_directory = tmp_path / "source-dir"
    source_directory.mkdir()
    target = tmp_path / "target"
    target.mkdir()

    with pytest.raises(ArtifactDeploymentError, match="source is not a regular file"):
        LocalArtifactDeployer().deploy(source_directory, target)

    source = tmp_path / "source.bin"
    source.write_bytes(b"data")
    not_directory = tmp_path / "not-directory"
    not_directory.write_text("x")
    with pytest.raises(ArtifactDeploymentError, match="is not a directory"):
        LocalArtifactDeployer().deploy(source, not_directory)


def test_deploy_rejects_source_larger_than_explicit_limit_without_writing(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.bin"
    source.write_bytes(b"12345")
    target = tmp_path / "target"
    target.mkdir()

    with pytest.raises(ArtifactDeploymentError, match="exceeds max_bytes"):
        LocalArtifactDeployer().deploy(source, target, max_bytes=4)

    assert not (target / "source.bin").exists()
    assert not list(target.glob(".source.bin.hostops-*"))


def test_deploy_enforces_whole_operation_timeout_before_commit_and_cleans_stage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source.bin"
    source.write_bytes(b"payload")
    target = tmp_path / "target"
    target.mkdir()
    ticks = iter((100.0, 101.0, 106.0))
    monkeypatch.setattr(bounds_module.time, "monotonic", lambda: next(ticks))

    with pytest.raises(ArtifactDeploymentError, match="whole-operation timeout before commit"):
        LocalArtifactDeployer().deploy(
            source,
            target,
            limits=ExecutionLimits(timeout_seconds=5.0),
        )

    assert not (target / "source.bin").exists()
    assert not list(target.glob(".source.bin.hostops-*"))


def test_deploy_timeout_after_commit_exposes_committed_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source.bin"
    source.write_bytes(b"payload")
    target = tmp_path / "target"
    target.mkdir()

    def timeout_after_commit(_path, _budget):
        raise bounds_module.ArtifactOperationTimeout

    monkeypatch.setattr(deploy_module, "_sync_directory", timeout_after_commit)

    with pytest.raises(ArtifactDeploymentError, match="after commit") as captured:
        LocalArtifactDeployer().deploy(source, target)

    assert captured.value.committed is True
    assert (target / "source.bin").read_bytes() == b"payload"
