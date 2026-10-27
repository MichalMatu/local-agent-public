"""Upload snapshots enforce the advertised size even when source files grow."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from host_ops_tests.unit.ssh.test_transfer import FakeTransferRunner, _target
from local_agent.host_ops.capabilities.remote.ssh import SshFileTransfer, SshTransferError
from local_agent.host_ops.capabilities.remote.ssh import local_stage
from local_agent.host_ops.core.execution import ExecutionLimits
from local_agent.host_ops.capabilities.remote.ssh.transfer import _TransferBudget


def test_growing_file_during_snapshot_never_writes_beyond_cap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source.bin"
    source.write_bytes(b"a")
    real_mkstemp = local_stage.tempfile.mkstemp
    real_unlink = Path.unlink
    real_open = Path.open
    removed_sizes: list[int] = []
    first_read = [True]

    def private_snapshot(*args, **kwargs):
        return real_mkstemp(*args, dir=tmp_path, **kwargs)

    def recorded_unlink(self, *args, **kwargs):
        if self.name.startswith("hostops-upload-") and self.exists():
            removed_sizes.append(self.stat().st_size)
        return real_unlink(self, *args, **kwargs)

    class _GrowingReader:
        def __init__(self, handle):
            self.handle = handle

        def __enter__(self):
            self.handle.__enter__()
            return self

        def __exit__(self, *args):
            return self.handle.__exit__(*args)

        def read(self, size=-1):
            data = self.handle.read(size)
            if first_read[0]:
                first_read[0] = False
                with real_open(source, "ab") as writer:
                    writer.write(b"b" * 1024)
            return data

    def open_with_growth(self, *args, **kwargs):
        handle = real_open(self, *args, **kwargs)
        if self == source and args and args[0] == "rb":
            return _GrowingReader(handle)
        return handle

    monkeypatch.setattr(local_stage.tempfile, "mkstemp", private_snapshot)
    monkeypatch.setattr(Path, "unlink", recorded_unlink)
    monkeypatch.setattr(Path, "open", open_with_growth)
    budget = _TransferBudget(ExecutionLimits(timeout_seconds=10.0))
    with pytest.raises(local_stage.LocalTransferBoundsError, match="max_bytes"):
        local_stage.bounded_upload_snapshot(
            source, 1, hashlib.sha256(b"a").hexdigest(), 4, budget,
        )
    assert removed_sizes == [1]
    assert not list(tmp_path.glob("hostops-upload-*.tmp"))


def test_original_source_growth_during_scp_cannot_expand_staged_upload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source.bin"
    source.write_bytes(b"abc")
    runner = FakeTransferRunner()
    original_scp = runner._scp
    observed_uploaded_sizes: list[int] = []

    def grow_after_snapshot(argv):
        snapshot_path = Path(argv[-2])
        observed_uploaded_sizes.append(snapshot_path.stat().st_size)
        source.write_bytes(b"a" * 1024)
        return original_scp(argv)

    monkeypatch.setattr(runner, "_scp", grow_after_snapshot)
    with pytest.raises(SshTransferError, match="max_bytes"):
        SshFileTransfer(runner).push(
            _target(), source, "/remote/output.bin", max_bytes=4,
        )
    assert observed_uploaded_sizes == [3]
    assert runner.scp_calls == 1
    assert runner.remote_files == {}
