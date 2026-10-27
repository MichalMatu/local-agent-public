from __future__ import annotations

import hashlib
import importlib
from pathlib import Path

import pytest

from local_agent.host_ops.capabilities.local.adb import AdbFileTransfer, AdbTransferError

transfer_module = importlib.import_module("local_agent.host_ops.capabilities.local.adb.transfer")
PAYLOAD = b"payload"
DIGEST = hashlib.sha256(PAYLOAD).hexdigest()


def _resolver(name: str) -> str | None:
    assert name == "adb"
    return "/opt/android/platform-tools/adb"


class DummyRunner:
    pass


class FakeRemote:
    def __init__(self, *, payload: bytes = PAYLOAD) -> None:
        self.payload = payload
        self.calls: list[tuple[object, ...]] = []
        self.size_values: list[int] = []
        self.sha_values: list[str] = []
        self.replaced_existing = False
        self.pull_payload = payload

    def require_ready(self) -> None:
        self.calls.append(("ready",))

    def require_directory(self, path: str) -> None:
        self.calls.append(("directory", path))

    def validate_destination(self, path: str, *, replace: bool) -> bool:
        self.calls.append(("validate-destination", path, replace))
        return self.replaced_existing

    def push(self, source: Path, destination: str) -> None:
        self.calls.append(("push", source, destination))

    def pull(self, source: str, destination: Path) -> None:
        self.calls.append(("pull", source, destination))
        destination.write_bytes(self.pull_payload)

    def size(self, path: str) -> int:
        self.calls.append(("size", path))
        if self.size_values:
            return self.size_values.pop(0)
        return len(self.payload)

    def sha256(self, path: str) -> str:
        self.calls.append(("sha256", path))
        if self.sha_values:
            return self.sha_values.pop(0)
        return hashlib.sha256(self.payload).hexdigest()

    def commit_stage(self, stage: str, destination: str, *, replace: bool) -> None:
        self.calls.append(("commit", stage, destination, replace))

    def require_regular_file(self, path: str, *, label: str) -> None:
        self.calls.append(("regular", path, label))

    def cleanup(self, stage: str, *, best_effort: bool = False) -> bool:
        self.calls.append(("cleanup", stage, best_effort))
        return True


def _transfer(monkeypatch: pytest.MonkeyPatch, remote: FakeRemote) -> AdbFileTransfer:
    monkeypatch.setattr(transfer_module, "AdbRemoteFiles", lambda *args, **kwargs: remote)
    return AdbFileTransfer(DummyRunner(), resolver=_resolver)


def test_push_verifies_stage_and_final_destination(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "input.bin"
    source.write_bytes(PAYLOAD)
    remote = FakeRemote()

    result = _transfer(monkeypatch, remote).push(
        "ABC123",
        source,
        "/sdcard/Download/input.bin",
    )

    assert result.direction == "push"
    assert result.serial == "ABC123"
    assert result.size_bytes == len(PAYLOAD)
    assert result.sha256 == DIGEST
    assert result.replaced_existing is False
    assert any(call[0] == "commit" for call in remote.calls)
    assert remote.calls[-1][0] == "sha256"


def test_push_reports_explicit_replace_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "input.bin"
    source.write_bytes(PAYLOAD)
    remote = FakeRemote()
    remote.replaced_existing = True

    result = _transfer(monkeypatch, remote).push(
        "ABC123",
        source,
        "/sdcard/input.bin",
        replace=True,
    )

    assert result.replaced_existing is True
    assert any(call[0] == "commit" and call[-1] is True for call in remote.calls)


def test_push_digest_mismatch_cleans_remote_stage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "input.bin"
    source.write_bytes(PAYLOAD)
    remote = FakeRemote()
    remote.sha_values = ["0" * 64]

    with pytest.raises(AdbTransferError, match="staging file"):
        _transfer(monkeypatch, remote).push("ABC123", source, "/sdcard/input.bin")

    assert any(call[0] == "cleanup" and call[-1] is True for call in remote.calls)


def test_push_rejects_oversize_source_before_remote_execution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "input.bin"
    source.write_bytes(PAYLOAD)
    remote = FakeRemote()

    with pytest.raises(AdbTransferError, match="exceeds max_bytes"):
        _transfer(monkeypatch, remote).push(
            "ABC123",
            source,
            "/sdcard/input.bin",
            max_bytes=2,
        )

    assert remote.calls == []


def test_push_rejects_invalid_serial_and_remote_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "input.bin"
    source.write_bytes(PAYLOAD)
    remote = FakeRemote()
    client = _transfer(monkeypatch, remote)

    with pytest.raises(AdbTransferError, match="serial"):
        client.push("--serial", source, "/sdcard/input.bin")
    with pytest.raises(AdbTransferError, match="absolute"):
        client.push("ABC123", source, "relative.bin")


def test_pull_verifies_remote_stability_and_commits_locally(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    destination = tmp_path / "output.bin"
    remote = FakeRemote()

    result = _transfer(monkeypatch, remote).pull(
        "ABC123",
        "/sdcard/output.bin",
        destination,
    )

    assert destination.read_bytes() == PAYLOAD
    assert result.direction == "pull"
    assert result.destination == str(destination.resolve())
    assert result.sha256 == DIGEST
    assert result.local_directory_synced in {True, False}
    assert [call[0] for call in remote.calls].count("size") == 2
    assert [call[0] for call in remote.calls].count("sha256") == 2


def test_pull_replace_overwrites_existing_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    destination = tmp_path / "output.bin"
    destination.write_bytes(b"old")
    remote = FakeRemote()

    result = _transfer(monkeypatch, remote).pull(
        "ABC123",
        "/sdcard/output.bin",
        destination,
        replace=True,
    )

    assert destination.read_bytes() == PAYLOAD
    assert result.replaced_existing is True


def test_pull_refuses_existing_destination_without_replace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    destination = tmp_path / "output.bin"
    destination.write_bytes(b"old")
    remote = FakeRemote()

    with pytest.raises(AdbTransferError, match="replace intent"):
        _transfer(monkeypatch, remote).pull(
            "ABC123",
            "/sdcard/output.bin",
            destination,
        )

    assert not any(call[0] == "pull" for call in remote.calls)


def test_pull_rejects_remote_source_mutation_and_cleans_stage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    destination = tmp_path / "output.bin"
    remote = FakeRemote()
    remote.size_values = [len(PAYLOAD), len(PAYLOAD) + 1]

    with pytest.raises(AdbTransferError, match="changed"):
        _transfer(monkeypatch, remote).pull(
            "ABC123",
            "/sdcard/output.bin",
            destination,
        )

    assert not destination.exists()
    assert not list(tmp_path.glob(".hostops-adb-*.tmp"))


def test_pull_rejects_download_digest_mismatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    destination = tmp_path / "output.bin"
    remote = FakeRemote(payload=b"expected")
    remote.pull_payload = b"corrupt"

    with pytest.raises(AdbTransferError, match="staging file"):
        _transfer(monkeypatch, remote).pull(
            "ABC123",
            "/sdcard/output.bin",
            destination,
        )

    assert not destination.exists()


def test_pull_no_clobber_race_is_reported(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    destination = tmp_path / "output.bin"
    remote = FakeRemote()

    def collide(_source, target):
        Path(target).write_bytes(b"other")
        raise FileExistsError

    monkeypatch.setattr(transfer_module.os, "link", collide)

    with pytest.raises(AdbTransferError, match="appeared"):
        _transfer(monkeypatch, remote).pull(
            "ABC123",
            "/sdcard/output.bin",
            destination,
        )

    assert destination.read_bytes() == b"other"


def test_missing_adb_and_invalid_max_bytes_fail_before_transfer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "input.bin"
    source.write_bytes(PAYLOAD)
    remote = FakeRemote()
    monkeypatch.setattr(transfer_module, "AdbRemoteFiles", lambda *args, **kwargs: remote)

    with pytest.raises(AdbTransferError, match="not found"):
        AdbFileTransfer(DummyRunner(), resolver=lambda _name: None).push(
            "ABC123", source, "/sdcard/input.bin"
        )

    with pytest.raises(AdbTransferError, match="max_bytes"):
        AdbFileTransfer(DummyRunner(), resolver=_resolver).push(
            "ABC123",
            source,
            "/sdcard/input.bin",
            max_bytes=0,
        )
