from __future__ import annotations

import errno
import hashlib
import importlib
from pathlib import Path

import pytest

from local_agent.host_ops.capabilities.local.adb import MAX_TRANSFER_BYTES, AdbFileTransfer, AdbTransferError

transfer_module = importlib.import_module("local_agent.host_ops.capabilities.local.adb.transfer")
PAYLOAD = b"payload"
DIGEST = hashlib.sha256(PAYLOAD).hexdigest()


class DummyRunner:
    pass


class FakeRemote:
    def __init__(self) -> None:
        self.calls: list[tuple[object, ...]] = []
        self.size_values: list[int] = []
        self.sha_values: list[str] = []
        self.pull_payload = PAYLOAD
        self.push_error: AdbTransferError | None = None
        self.commit_error: AdbTransferError | None = None
        self.cleanup_ok = True

    def require_ready(self) -> None:
        self.calls.append(("ready",))

    def require_directory(self, path: str) -> None:
        self.calls.append(("directory", path))

    def validate_destination(self, path: str, *, replace: bool) -> bool:
        self.calls.append(("validate", path, replace))
        return False

    def push(self, source: Path, destination: str) -> None:
        self.calls.append(("push", source, destination))
        if self.push_error is not None:
            raise self.push_error

    def pull(self, source: str, destination: Path) -> None:
        self.calls.append(("pull", source, destination))
        destination.write_bytes(self.pull_payload)

    def size(self, path: str) -> int:
        self.calls.append(("size", path))
        if self.size_values:
            return self.size_values.pop(0)
        return len(PAYLOAD)

    def sha256(self, path: str) -> str:
        self.calls.append(("sha256", path))
        if self.sha_values:
            return self.sha_values.pop(0)
        return DIGEST

    def commit_stage(self, stage: str, destination: str, *, replace: bool) -> None:
        self.calls.append(("commit", stage, destination, replace))
        if self.commit_error is not None:
            raise self.commit_error

    def require_regular_file(self, path: str, *, label: str) -> None:
        self.calls.append(("regular", path, label))

    def cleanup(self, stage: str, *, best_effort: bool = False) -> bool:
        self.calls.append(("cleanup", stage, best_effort))
        return self.cleanup_ok


def _resolver(name: str) -> str | None:
    assert name == "adb"
    return "/opt/android/platform-tools/adb"


def _client(monkeypatch: pytest.MonkeyPatch, remote: FakeRemote) -> AdbFileTransfer:
    monkeypatch.setattr(transfer_module, "AdbRemoteFiles", lambda *args, **kwargs: remote)
    return AdbFileTransfer(DummyRunner(), resolver=_resolver)


def test_push_rejects_post_commit_size_mismatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "input.bin"
    source.write_bytes(PAYLOAD)
    remote = FakeRemote()
    remote.size_values = [len(PAYLOAD), len(PAYLOAD) + 1]

    with pytest.raises(AdbTransferError, match="post-commit size") as exc_info:
        _client(monkeypatch, remote).push("ABC", source, "/sdcard/input.bin")

    assert exc_info.value.action_attempted is True
    assert exc_info.value.committed is True
    assert exc_info.value.cleanup_failed is False


def test_push_rejects_post_commit_digest_mismatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "input.bin"
    source.write_bytes(PAYLOAD)
    remote = FakeRemote()
    remote.sha_values = [DIGEST, "0" * 64]

    with pytest.raises(AdbTransferError, match="post-commit SHA-256"):
        _client(monkeypatch, remote).push("ABC", source, "/sdcard/input.bin")


def test_push_commit_failure_reports_attempted_unknown_and_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "input.bin"
    source.write_bytes(PAYLOAD)
    remote = FakeRemote()
    remote.commit_error = AdbTransferError("commit timed out", action_attempted=True)
    remote.cleanup_ok = False

    with pytest.raises(AdbTransferError, match="commit timed out") as exc_info:
        _client(monkeypatch, remote).push("ABC", source, "/sdcard/input.bin")

    assert exc_info.value.action_attempted is True
    assert exc_info.value.committed is False
    assert exc_info.value.cleanup_failed is True
    assert any(call[0] == "cleanup" for call in remote.calls)


def test_pull_sync_failure_reports_committed_destination(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    destination = tmp_path / "out.bin"
    remote = FakeRemote()

    def fail_sync(_path: Path) -> bool:
        raise OSError(errno.EIO, "sync failed")

    monkeypatch.setattr(transfer_module, "_sync_directory", fail_sync)
    with pytest.raises(AdbTransferError, match="sync failed") as exc_info:
        _client(monkeypatch, remote).pull("ABC", "/sdcard/out.bin", destination)

    assert destination.read_bytes() == PAYLOAD
    assert exc_info.value.action_attempted is True
    assert exc_info.value.committed is True


def test_pull_rejects_remote_source_larger_than_limit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    remote = FakeRemote()
    remote.size_values = [len(PAYLOAD)]

    with pytest.raises(AdbTransferError, match="exceeds max_bytes"):
        _client(monkeypatch, remote).pull(
            "ABC",
            "/sdcard/out.bin",
            tmp_path / "out.bin",
            max_bytes=2,
        )

    assert not any(call[0] == "pull" for call in remote.calls)


def test_push_rejects_symlink_and_non_regular_local_sources(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    remote = FakeRemote()
    target = tmp_path / "real.bin"
    target.write_bytes(PAYLOAD)
    symlink = tmp_path / "link.bin"
    symlink.symlink_to(target)
    client = _client(monkeypatch, remote)

    with pytest.raises(AdbTransferError, match="symbolic link"):
        client.push("ABC", symlink, "/sdcard/link.bin")
    with pytest.raises(AdbTransferError, match="not a regular file"):
        client.push("ABC", tmp_path, "/sdcard/dir.bin")


def test_pull_rejects_local_destination_directory_and_symlink(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    remote = FakeRemote()
    client = _client(monkeypatch, remote)
    directory_destination = tmp_path / "existing"
    directory_destination.mkdir()

    with pytest.raises(AdbTransferError, match="not a regular file"):
        client.pull("ABC", "/sdcard/out.bin", directory_destination, replace=True)

    real = tmp_path / "real.bin"
    real.write_bytes(b"old")
    link = tmp_path / "link.bin"
    link.symlink_to(real)
    with pytest.raises(AdbTransferError, match="symbolic link"):
        client.pull("ABC", "/sdcard/out.bin", link, replace=True)


def test_pull_reports_generic_local_link_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    destination = tmp_path / "out.bin"
    remote = FakeRemote()

    def fail_link(_source, _destination):
        raise OSError(errno.EPERM, "links disabled")

    monkeypatch.setattr(transfer_module.os, "link", fail_link)
    with pytest.raises(AdbTransferError, match="no-clobber commit failed"):
        _client(monkeypatch, remote).pull("ABC", "/sdcard/out.bin", destination)


def test_pull_reports_unsupported_directory_fsync_without_failing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    destination = tmp_path / "out.bin"
    remote = FakeRemote()
    original_fsync = transfer_module.os.fsync
    calls = 0

    def fsync_once_then_unsupported(fd):
        nonlocal calls
        calls += 1
        if calls == 1:
            return original_fsync(fd)
        raise OSError(errno.EINVAL, "unsupported")

    monkeypatch.setattr(transfer_module.os, "fsync", fsync_once_then_unsupported)
    result = _client(monkeypatch, remote).pull("ABC", "/sdcard/out.bin", destination)

    assert result.local_directory_synced is False
    assert destination.read_bytes() == PAYLOAD


def test_transfer_budget_and_max_bytes_validation_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    budget = transfer_module._TransferBudget(transfer_module.ExecutionLimits(timeout_seconds=1.0))
    monkeypatch.setattr(transfer_module.time, "monotonic", lambda: float("inf"))
    with pytest.raises(AdbTransferError, match="whole-operation timeout"):
        budget.remaining()

    for invalid in (True, 0, MAX_TRANSFER_BYTES + 1):
        with pytest.raises(AdbTransferError, match="max_bytes"):
            transfer_module._validate_max_bytes(invalid)
