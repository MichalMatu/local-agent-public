"""Deterministic whole-operation deadlines and source-growth regressions."""

from __future__ import annotations

from pathlib import Path

import pytest

from host_ops_tests.unit.ssh.test_transfer import FakeTransferRunner, _target
from local_agent.host_ops.capabilities.remote.ssh import SshFileTransfer, SshTransferError
from local_agent.host_ops.capabilities.remote.ssh import transfer as transfer_module
from local_agent.host_ops.core.execution import ExecutionLimits


class _ClockReader:
    def __init__(self, handle, clock: list[float]):
        self.handle = handle
        self.clock = clock

    def __enter__(self):
        self.handle.__enter__()
        return self

    def __exit__(self, *args):
        return self.handle.__exit__(*args)

    def read(self, size=-1):
        data = self.handle.read(size)
        self.clock[0] += 2.0
        return data


def test_hashing_checks_monotonic_deadline_after_each_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "payload.bin"
    source.write_bytes(b"abc")
    clock = [0.0]
    monkeypatch.setattr(transfer_module.time, "monotonic", lambda: clock[0])
    original_open = Path.open

    def advancing_open(self, *args, **kwargs):
        handle = original_open(self, *args, **kwargs)
        if self == source and args and args[0] == "rb":
            return _ClockReader(handle, clock)
        return handle

    monkeypatch.setattr(Path, "open", advancing_open)
    budget = transfer_module._TransferBudget(ExecutionLimits(timeout_seconds=1.0))
    with pytest.raises(SshTransferError, match="whole-operation timeout"):
        transfer_module._sha256_file(source, budget=budget, max_bytes=10)


def test_pull_timeout_during_staged_hash_never_commits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    destination = tmp_path / "download.bin"
    destination.write_bytes(b"previous data")
    runner = FakeTransferRunner({"/remote/result.bin": b"new data"})
    clock = [0.0]
    monkeypatch.setattr(transfer_module.time, "monotonic", lambda: clock[0])
    original = transfer_module._sha256_file

    def delayed_staging_hash(path: Path, **kwargs):
        digest = original(path, **kwargs)
        if path.name.startswith(".hostops-download-"):
            clock[0] = 2.0
        return digest

    monkeypatch.setattr(transfer_module, "_sha256_file", delayed_staging_hash)
    with pytest.raises(SshTransferError, match="whole-operation timeout") as caught:
        SshFileTransfer(runner).pull(
            _target(), "/remote/result.bin", destination,
            replace=True, limits=ExecutionLimits(timeout_seconds=1.0),
        )
    assert caught.value.committed is False
    assert destination.read_bytes() == b"previous data"


def test_push_expired_after_commit_reports_real_effect(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source.bin"
    source.write_bytes(b"new data")
    destination = "/remote/output.bin"
    runner = FakeTransferRunner()
    transfer = SshFileTransfer(runner)
    clock = [0.0]
    monkeypatch.setattr(transfer_module.time, "monotonic", lambda: clock[0])
    original = transfer._remote_sha256

    def expire_after_final_hash(target, path, budget):
        digest = original(target, path, budget)
        if path == destination:
            clock[0] = 2.0
        return digest

    monkeypatch.setattr(transfer, "_remote_sha256", expire_after_final_hash)
    with pytest.raises(SshTransferError, match="whole-operation timeout") as caught:
        transfer.push(
            _target(), source, destination,
            limits=ExecutionLimits(timeout_seconds=1.0),
        )
    assert caught.value.committed is True
    assert runner.remote_files[destination] == b"new data"


def test_source_growth_is_rejected_before_upload_not_after_scp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source.bin"
    source.write_bytes(b"a")
    runner = FakeTransferRunner()
    original = transfer_module._sha256_file
    grown = [False]

    def grow_before_hash(path: Path, **kwargs):
        if path == source.resolve() and not grown[0]:
            grown[0] = True
            source.write_bytes(b"a" * 100)
        return original(path, **kwargs)

    monkeypatch.setattr(transfer_module, "_sha256_file", grow_before_hash)
    with pytest.raises(SshTransferError, match="max_bytes"):
        SshFileTransfer(runner).push(
            _target(), source, "/remote/output.bin", max_bytes=4,
        )
    assert grown[0]
    assert runner.scp_calls == 0
    assert runner.remote_files == {}
