from __future__ import annotations

import hashlib
import os
import shlex
from pathlib import Path

import pytest

from local_agent.host_ops.capabilities.remote.ssh import (
    SYSTEM_SCP_EXECUTABLE,
    SshFileTransfer,
    SshTransferError,
)
from local_agent.host_ops.core.config import HostTarget
from local_agent.host_ops.core.execution import ProcessResult, ProcessState

_TEST_IDENTITY = "/tmp/host_ops_test_key"


def _result(stdout: str = "", *, exit_code: int = 0, stderr: str = "") -> ProcessResult:
    return ProcessResult(
        state=ProcessState.COMPLETED,
        exit_code=exit_code,
        stdout=stdout,
        stderr=stderr,
        duration_seconds=0.001,
    )


def _target() -> HostTarget:
    return HostTarget(
        alias="phone",
        host="example.test",
        port=8022,
        user="user",
        identity_file=_TEST_IDENTITY,
    )


class FakeTransferRunner:
    def __init__(
        self,
        remote_files: dict[str, bytes] | None = None,
        *,
        corrupt_push: bool = False,
        hardlink_error: str | None = None,
        destination_appears_on_link: bytes | None = None,
        rm_error: bool = False,
    ) -> None:
        self.remote_files = dict(remote_files or {})
        self.remote_directories = {"/remote"}
        self.corrupt_push = corrupt_push
        self.hardlink_error = hardlink_error
        self.destination_appears_on_link = destination_appears_on_link
        self.rm_error = rm_error
        self.scp_calls = 0
        self.binary_stream_calls = 0
        self.commands: list[tuple[str, ...]] = []

    def run(self, command, *, limits=None, **kwargs):
        argv = tuple(command)
        self.commands.append(argv)
        if "binary_stdout_fd" in kwargs:
            self.binary_stream_calls += 1
            return self._binary_stream(argv, kwargs)
        if argv[0] == SYSTEM_SCP_EXECUTABLE:
            self.scp_calls += 1
            return self._scp(argv)
        return self._ssh(argv)

    def _binary_stream(self, argv, kwargs) -> ProcessResult:
        remote = tuple(shlex.split(argv[-1]))
        assert remote[0] == "cat"
        payload = self.remote_files[remote[-1]]
        cap = kwargs["binary_stdout_limit"]
        os.write(kwargs["binary_stdout_fd"], payload[:cap])
        if len(payload) > cap:
            return ProcessResult(
                state=ProcessState.OUTPUT_LIMIT_EXCEEDED,
                exit_code=-15,
                stdout="",
                stderr="",
                duration_seconds=0.001,
                error="binary stdout exceeded byte limit",
            )
        return _result()

    def _scp(self, argv: tuple[str, ...]) -> ProcessResult:
        source, destination = argv[-2:]
        if ":/" in destination:
            remote_path = "/" + destination.split(":/", 1)[1]
            payload = Path(source).read_bytes()
            self.remote_files[remote_path] = payload + (b"corrupt" if self.corrupt_push else b"")
            return _result()
        remote_path = "/" + source.split(":/", 1)[1]
        Path(destination).write_bytes(self.remote_files[remote_path])
        return _result()

    def _ssh(self, argv: tuple[str, ...]) -> ProcessResult:
        remote = tuple(shlex.split(argv[-1]))
        program = remote[0]
        if program == "test":
            operator, path = remote[1], remote[2]
            if operator == "-L":
                return _result(exit_code=1)
            if operator == "-d":
                return _result(exit_code=0 if path in self.remote_directories else 1)
            if operator == "-e":
                return _result(exit_code=0 if path in self.remote_files else 1)
            if operator == "-f":
                return _result(exit_code=0 if path in self.remote_files else 1)
        if program == "wc":
            path = remote[-1]
            payload = self.remote_files[path]
            return _result(f"{len(payload)} {path}\n")
        if program == "sh":
            path = remote[-1]
            digest = hashlib.sha256(self.remote_files[path]).hexdigest()
            return _result(f"{digest}  {path}\n")
        if program == "ln":
            assert remote[1:3] == ("-T", "--")
            source, destination = remote[-2:]
            if self.destination_appears_on_link is not None:
                self.remote_files[destination] = self.destination_appears_on_link
                return _result(exit_code=1, stderr="File exists")
            if self.hardlink_error is not None:
                return _result(exit_code=1, stderr=self.hardlink_error)
            if destination in self.remote_files:
                return _result(exit_code=1, stderr="File exists")
            self.remote_files[destination] = self.remote_files[source]
            return _result()
        if program == "mv":
            assert remote[1:3] == ("-T", "--")
            source, destination = remote[-2:]
            self.remote_files[destination] = self.remote_files.pop(source)
            return _result()
        if program == "rm":
            if self.rm_error:
                return _result(exit_code=1, stderr="cleanup failed")
            self.remote_files.pop(remote[-1], None)
            return _result()
        raise AssertionError(f"unexpected remote command: {remote!r}")


def test_push_stages_verifies_and_commits_without_replace(tmp_path: Path) -> None:
    source = tmp_path / "firmware.bin"
    source.write_bytes(b"firmware-data")
    runner = FakeTransferRunner()

    result = SshFileTransfer(runner).push(_target(), source, "/remote/firmware.bin")

    assert result.direction == "push"
    assert result.target == "phone"
    assert result.destination == "/remote/firmware.bin"
    assert result.size_bytes == len(b"firmware-data")
    assert result.sha256 == hashlib.sha256(b"firmware-data").hexdigest()
    assert result.replaced_existing is False
    assert result.staging_cleaned is True
    assert runner.remote_files["/remote/firmware.bin"] == b"firmware-data"
    assert not any(".hostops-upload-" in path for path in runner.remote_files)
    assert runner.scp_calls == 1


def test_push_refuses_unsafe_fallback_when_hardlinks_are_denied(
    tmp_path: Path,
) -> None:
    source = tmp_path / "firmware.bin"
    source.write_bytes(b"firmware-data")
    runner = FakeTransferRunner(hardlink_error="Permission denied")

    with pytest.raises(SshTransferError) as caught:
        SshFileTransfer(runner).push(_target(), source, "/remote/firmware.bin")

    assert caught.value.code == "ssh_remote_capability_unsupported"
    assert caught.value.phase == "remote_commit"
    assert runner.remote_files == {}
    assert not any(
        tuple(shlex.split(command[-1])[:2]) == ("mv", "-n")
        for command in runner.commands
        if command[0] != SYSTEM_SCP_EXECUTABLE
    )

def test_push_noclobber_fallback_preserves_destination_that_appears(
    tmp_path: Path,
) -> None:
    source = tmp_path / "firmware.bin"
    source.write_bytes(b"firmware-data")
    runner = FakeTransferRunner(destination_appears_on_link=b"racer")

    with pytest.raises(SshTransferError, match="destination appeared"):
        SshFileTransfer(runner).push(_target(), source, "/remote/firmware.bin")

    assert runner.remote_files["/remote/firmware.bin"] == b"racer"
    assert not any(".hostops-upload-" in path for path in runner.remote_files)


def test_push_requires_explicit_replace_for_existing_remote_file(tmp_path: Path) -> None:
    source = tmp_path / "firmware.bin"
    source.write_bytes(b"new")
    runner = FakeTransferRunner({"/remote/firmware.bin": b"old"})

    with pytest.raises(SshTransferError, match="explicit replace intent"):
        SshFileTransfer(runner).push(_target(), source, "/remote/firmware.bin")

    assert runner.remote_files["/remote/firmware.bin"] == b"old"
    assert runner.scp_calls == 0


def test_push_replace_is_verified_after_atomic_move(tmp_path: Path) -> None:
    source = tmp_path / "firmware.bin"
    source.write_bytes(b"new")
    runner = FakeTransferRunner({"/remote/firmware.bin": b"old"})

    result = SshFileTransfer(runner).push(
        _target(),
        source,
        "/remote/firmware.bin",
        replace=True,
    )

    assert result.replaced_existing is True
    assert runner.remote_files["/remote/firmware.bin"] == b"new"


def test_push_rejects_corrupted_staging_and_cleans_it(tmp_path: Path) -> None:
    source = tmp_path / "firmware.bin"
    source.write_bytes(b"payload")
    runner = FakeTransferRunner(corrupt_push=True)

    with pytest.raises(SshTransferError, match="staging file failed"):
        SshFileTransfer(runner).push(_target(), source, "/remote/firmware.bin")

    assert "/remote/firmware.bin" not in runner.remote_files
    assert not any(".hostops-upload-" in path for path in runner.remote_files)


@pytest.mark.parametrize("state", [ProcessState.COMPLETED, ProcessState.TIMED_OUT])
def test_interrupted_upload_preserves_destination_and_can_be_retried(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, state: ProcessState,
) -> None:
    source = tmp_path / "archive.bin"
    source.write_bytes(b"complete-data")
    destination = "/remote/archive.bin"
    runner = FakeTransferRunner({destination: b"original"})
    original_scp = runner._scp

    def interrupted(argv):
        stage = "/" + argv[-1].split(":/", 1)[1]
        runner.remote_files[stage] = b"partial"
        return ProcessResult(
            state=state, exit_code=255, stdout="", stderr="Connection lost",
            duration_seconds=0.001,
        )

    monkeypatch.setattr(runner, "_scp", interrupted)
    transfer = SshFileTransfer(runner)
    with pytest.raises(SshTransferError, match="SCP upload.*Connection lost") as failure:
        transfer.push(_target(), source, destination, replace=True)
    assert failure.value.action_attempted is True
    assert failure.value.committed is False
    assert runner.remote_files == {destination: b"original"}
    assert runner.scp_calls == 1  # No automatic transfer retry.
    monkeypatch.setattr(runner, "_scp", original_scp)
    result = transfer.push(_target(), source, destination, replace=True)
    assert result.sha256 == hashlib.sha256(source.read_bytes()).hexdigest()
    assert runner.remote_files == {destination: b"complete-data"}
    assert runner.scp_calls == 2


def test_wrong_sha256_with_matching_size_never_commits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "archive.bin"
    source.write_bytes(b"new-data")
    runner = FakeTransferRunner({"/remote/archive.bin": b"original"})
    original_scp = runner._scp

    def corrupted(argv):
        result = original_scp(argv)
        stage = "/" + argv[-1].split(":/", 1)[1]
        runner.remote_files[stage] = b"bad-data"
        return result

    monkeypatch.setattr(runner, "_scp", corrupted)
    with pytest.raises(SshTransferError, match="SHA-256") as failure:
        SshFileTransfer(runner).push(_target(), source, "/remote/archive.bin", replace=True)
    assert failure.value.committed is False
    assert runner.remote_files == {"/remote/archive.bin": b"original"}


def test_ssh_connection_failure_prevents_upload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "archive.bin"
    source.write_bytes(b"payload")
    runner = FakeTransferRunner()
    monkeypatch.setattr(
        runner, "_ssh", lambda _argv: _result(exit_code=255, stderr="Connection refused"),
    )
    with pytest.raises(SshTransferError, match="remote test.*Connection refused") as failure:
        SshFileTransfer(runner).push(_target(), source, "/remote/archive.bin")
    assert failure.value.code == "ssh_transport_failed"
    assert failure.value.action_attempted is False
    assert failure.value.committed is False
    assert runner.scp_calls == 0
    assert runner.remote_files == {}


def test_successful_upload_retry_does_not_overwrite_existing_data(tmp_path: Path) -> None:
    source = tmp_path / "archive.bin"
    source.write_bytes(b"verified-data")
    runner = FakeTransferRunner()
    transfer = SshFileTransfer(runner)
    transfer.push(_target(), source, "/remote/archive.bin")
    with pytest.raises(SshTransferError, match="explicit replace intent"):
        transfer.push(_target(), source, "/remote/archive.bin")
    assert runner.remote_files == {"/remote/archive.bin": b"verified-data"}
    assert runner.scp_calls == 1
    assert all(command[0] in ("/usr/bin/ssh", "/usr/bin/scp") for command in runner.commands)


def test_push_post_commit_verification_failure_reports_committed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "firmware.bin"
    source.write_bytes(b"firmware-data")
    runner = FakeTransferRunner()
    transfer = SshFileTransfer(runner)
    original_size = transfer._remote_size

    def mismatched_final_size(target, path, budget):
        size = original_size(target, path, budget)
        return size + 1 if path == "/remote/firmware.bin" else size

    monkeypatch.setattr(transfer, "_remote_size", mismatched_final_size)
    with pytest.raises(SshTransferError, match="post-commit verification") as exc_info:
        transfer.push(_target(), source, "/remote/firmware.bin")

    assert exc_info.value.action_attempted is True
    assert exc_info.value.committed is True
    assert exc_info.value.cleanup_failed is False


def test_push_cleanup_failure_after_hardlink_commit_preserves_effects(
    tmp_path: Path,
) -> None:
    source = tmp_path / "firmware.bin"
    source.write_bytes(b"firmware-data")
    runner = FakeTransferRunner(rm_error=True)

    with pytest.raises(SshTransferError, match="staging cleanup") as exc_info:
        SshFileTransfer(runner).push(_target(), source, "/remote/firmware.bin")

    assert runner.remote_files["/remote/firmware.bin"] == b"firmware-data"
    assert exc_info.value.action_attempted is True
    assert exc_info.value.committed is True
    assert exc_info.value.cleanup_failed is True


def test_push_ambiguous_commit_failure_does_not_claim_committed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "firmware.bin"
    source.write_bytes(b"firmware-data")
    runner = FakeTransferRunner()
    transfer = SshFileTransfer(runner)

    def fail_commit(*_args, **_kwargs):
        raise SshTransferError("commit timed out", action_attempted=True)

    monkeypatch.setattr(
        "local_agent.host_ops.capabilities.remote.ssh.transfer._commit_remote_no_clobber",
        fail_commit,
    )
    with pytest.raises(SshTransferError, match="commit timed out") as exc_info:
        transfer.push(_target(), source, "/remote/firmware.bin")

    assert exc_info.value.action_attempted is True
    assert exc_info.value.committed is False


def test_push_rejects_source_over_max_bytes(tmp_path: Path) -> None:
    source = tmp_path / "large.bin"
    source.write_bytes(b"12345")

    with pytest.raises(SshTransferError, match="exceeds max_bytes"):
        SshFileTransfer(FakeTransferRunner()).push(
            _target(),
            source,
            "/remote/large.bin",
            max_bytes=4,
        )


def test_push_rejects_directory_source_before_hashing(tmp_path: Path) -> None:
    source = tmp_path / "directory"
    source.mkdir()

    with pytest.raises(SshTransferError, match="local source is not a regular file"):
        SshFileTransfer(FakeTransferRunner()).push(
            _target(),
            source,
            "/remote/source.bin",
        )


def test_pull_stages_verifies_and_commits_locally(tmp_path: Path) -> None:
    payload = b"remote-result"
    runner = FakeTransferRunner({"/remote/result.bin": payload})
    destination = tmp_path / "result.bin"

    result = SshFileTransfer(runner).pull(_target(), "/remote/result.bin", destination)

    assert destination.read_bytes() == payload
    assert result.direction == "pull"
    assert result.destination == str(destination.resolve())
    assert result.sha256 == hashlib.sha256(payload).hexdigest()
    assert result.replaced_existing is False
    assert result.local_directory_synced is True
    assert runner.binary_stream_calls == 1
    assert runner.scp_calls == 0
    assert not list(tmp_path.glob(".hostops-download-*.tmp"))


def test_pull_requires_explicit_replace_for_existing_local_file(tmp_path: Path) -> None:
    runner = FakeTransferRunner({"/remote/result.bin": b"new"})
    destination = tmp_path / "result.bin"
    destination.write_bytes(b"old")

    with pytest.raises(SshTransferError, match="explicit replace intent"):
        SshFileTransfer(runner).pull(_target(), "/remote/result.bin", destination)

    assert destination.read_bytes() == b"old"
    assert runner.scp_calls == 0


def test_pull_replace_commits_verified_file(tmp_path: Path) -> None:
    runner = FakeTransferRunner({"/remote/result.bin": b"new"})
    destination = tmp_path / "result.bin"
    destination.write_bytes(b"old")

    result = SshFileTransfer(runner).pull(
        _target(),
        "/remote/result.bin",
        destination,
        replace=True,
    )

    assert destination.read_bytes() == b"new"
    assert result.replaced_existing is True


def test_pull_sync_failure_reports_committed_destination(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = b"remote-result"
    runner = FakeTransferRunner({"/remote/result.bin": payload})
    destination = tmp_path / "result.bin"

    def fail_sync(_path: Path) -> bool:
        raise OSError("sync failed")

    monkeypatch.setattr(
        "local_agent.host_ops.capabilities.remote.ssh.transfer._sync_directory",
        fail_sync,
    )
    with pytest.raises(SshTransferError, match="sync failed") as exc_info:
        SshFileTransfer(runner).pull(_target(), "/remote/result.bin", destination)

    assert destination.read_bytes() == payload
    assert exc_info.value.action_attempted is True
    assert exc_info.value.committed is True


def test_pull_rejects_remote_file_over_max_bytes(tmp_path: Path) -> None:
    runner = FakeTransferRunner({"/remote/result.bin": b"12345"})

    with pytest.raises(SshTransferError, match="exceeds max_bytes"):
        SshFileTransfer(runner).pull(
            _target(),
            "/remote/result.bin",
            tmp_path / "result.bin",
            max_bytes=4,
        )

    assert runner.scp_calls == 0


@pytest.mark.parametrize(
    "path",
    ["relative.bin", "//remote/file.bin", "/remote/../secret", "/remote/file with space.bin"],
)
def test_transfer_rejects_unsafe_remote_paths(tmp_path: Path, path: str) -> None:
    source = tmp_path / "source.bin"
    source.write_bytes(b"data")

    with pytest.raises(SshTransferError, match="remote path"):
        SshFileTransfer(FakeTransferRunner()).push(_target(), source, path)


@pytest.mark.parametrize("payload,max_bytes", [
    (b"", 4),
    (b"ABCD", 4),
    (bytes(range(256)), 256),
])
def test_binary_pull_exact_cap_and_raw_bytes(
    tmp_path: Path, payload: bytes, max_bytes: int,
) -> None:
    runner = FakeTransferRunner({"/remote/binary.bin": payload})
    destination = tmp_path / "binary.bin"
    result = SshFileTransfer(runner).pull(
        _target(), "/remote/binary.bin", destination, max_bytes=max_bytes,
    )
    assert result.sha256 == hashlib.sha256(payload).hexdigest()
    assert destination.read_bytes() == payload
    assert runner.scp_calls == 0
    assert runner.binary_stream_calls == 1


def test_growing_download_never_writes_beyond_limit_or_replaces_destination(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = "/remote/binary.bin"
    destination = tmp_path / "binary.bin"
    destination.write_bytes(b"previous")
    runner = FakeTransferRunner({source: b"x"})
    original = runner._binary_stream
    observed_sizes = []

    def stream_after_growth(argv, kwargs):
        # The remote preflight saw one byte; source changes before the pipe.
        runner.remote_files[source] = b"x" * 1024
        result = original(argv, kwargs)
        observed_sizes.append(os.fstat(kwargs["binary_stdout_fd"]).st_size)
        return result

    monkeypatch.setattr(runner, "_binary_stream", stream_after_growth)
    with pytest.raises(SshTransferError) as caught:
        SshFileTransfer(runner).pull(
            _target(), source, destination, replace=True, max_bytes=4,
        )
    assert caught.value.code == "ssh_size_limit_exceeded"
    assert observed_sizes == [4]
    assert destination.read_bytes() == b"previous"
    assert not list(tmp_path.glob(".hostops-download-*.tmp"))
    assert runner.scp_calls == 0


def test_binary_partial_nonzero_exit_preserves_old_destination(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    destination = tmp_path / "binary.bin"
    destination.write_bytes(b"previous")
    runner = FakeTransferRunner({"/remote/binary.bin": b"new-data"})

    def interrupted(_argv, kwargs):
        os.write(kwargs["binary_stdout_fd"], b"new")
        return _result(exit_code=7, stderr="remote cat failed")

    monkeypatch.setattr(runner, "_binary_stream", interrupted)
    with pytest.raises(SshTransferError) as caught:
        SshFileTransfer(runner).pull(
            _target(), "/remote/binary.bin", destination,
            replace=True, max_bytes=16,
        )
    assert caught.value.code == "ssh_remote_command_failed"
    assert not caught.value.committed
    assert destination.read_bytes() == b"previous"
    assert not list(tmp_path.glob(".hostops-download-*.tmp"))


@pytest.mark.parametrize("direction", ["push", "pull"])
def test_process_spawn_failure_reports_no_transfer_attempt_and_cleans_local_stage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, direction: str,
) -> None:
    payload = b"source bytes"
    local_source = tmp_path / "source.bin"
    local_source.write_bytes(payload)
    destination = tmp_path / "download.bin"
    runner = FakeTransferRunner({"/remote/source.bin": payload})
    original_run = runner.run
    attempted_processes = []

    def spawn_refusal(command, *, limits=None, **kwargs):
        if command[0] == SYSTEM_SCP_EXECUTABLE or "binary_stdout_fd" in kwargs:
            attempted_processes.append(tuple(command))
            return ProcessResult(
                state=ProcessState.SPAWN_FAILED,
                exit_code=None,
                stdout="",
                stderr="",
                duration_seconds=0.0,
                error="OSError: executable could not be started",
            )
        return original_run(command, limits=limits, **kwargs)

    monkeypatch.setattr(runner, "run", spawn_refusal)
    transfer = SshFileTransfer(runner)
    with pytest.raises(SshTransferError) as caught:
        if direction == "push":
            transfer.push(_target(), local_source, "/remote/output.bin")
        else:
            transfer.pull(_target(), "/remote/source.bin", destination)

    assert len(attempted_processes) == 1
    assert caught.value.code == "ssh_process_spawn_failed"
    assert caught.value.action_attempted is False
    assert caught.value.committed is False
    assert caught.value.cleanup_failed is False
    assert "/remote/output.bin" not in runner.remote_files
    assert not destination.exists()
    assert not list(tmp_path.glob(".hostops-download-*.tmp"))


def test_local_download_stage_open_refusal_is_not_claimed_as_ssh_attempt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from local_agent.host_ops.capabilities.remote.ssh import transfer as transfer_module

    runner = FakeTransferRunner({"/remote/source.bin": b"payload"})
    destination = tmp_path / "download.bin"
    real_open = os.open

    def deny_stage_open(path, flags, *args, **kwargs):
        if str(path).endswith(".tmp") and ".hostops-download-" in str(path):
            raise PermissionError("local staging not permitted")
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(transfer_module.os, "open", deny_stage_open)
    with pytest.raises(SshTransferError) as caught:
        SshFileTransfer(runner).pull(
            _target(), "/remote/source.bin", destination,
        )

    assert caught.value.action_attempted is False
    assert caught.value.code == "ssh_local_io_failed"
    assert runner.binary_stream_calls == 0
    assert not destination.exists()
    assert not list(tmp_path.glob(".hostops-download-*.tmp"))



def test_pull_connection_failure_preflight_has_no_transfer_effect(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = FakeTransferRunner({"/remote/source.bin": b"original"})
    monkeypatch.setattr(
        runner, "_ssh", lambda _argv: _result(exit_code=255, stderr="Connection refused"),
    )
    destination = tmp_path / "destination.bin"
    with pytest.raises(SshTransferError, match="remote test.*Connection refused") as caught:
        SshFileTransfer(runner).pull(
            _target(), "/remote/source.bin", destination,
        )
    assert caught.value.code == "ssh_transport_failed"
    assert caught.value.action_attempted is False
    assert caught.value.committed is False
    assert runner.binary_stream_calls == 0
    assert runner.scp_calls == 0
    assert not destination.exists()
    assert not list(tmp_path.glob(".hostops-download-*.tmp"))


def test_download_exclusive_stage_collision_does_not_delete_foreign_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from local_agent.host_ops.capabilities.remote.ssh import transfer as transfer_module

    runner = FakeTransferRunner({"/remote/source.bin": b"remote bytes"})
    destination = tmp_path / "download.bin"
    stage = tmp_path / ".hostops-download-deadbeef.tmp"
    stage.write_bytes(b"foreign file: must be preserved")
    monkeypatch.setattr(transfer_module.secrets, "token_hex", lambda _n: "deadbeef")

    with pytest.raises(SshTransferError) as caught:
        SshFileTransfer(runner).pull(
            _target(), "/remote/source.bin", destination,
        )

    assert caught.value.code == "ssh_local_io_failed"
    assert caught.value.action_attempted is False
    assert caught.value.committed is False
    assert caught.value.cleanup_failed is False
    assert stage.read_bytes() == b"foreign file: must be preserved"
    assert not destination.exists()
    assert runner.binary_stream_calls == 0


@pytest.mark.parametrize(
    ("program", "malformed", "message"),
    [
        ("wc", "not-an-integer", "invalid output"),
        ("wc", "-1 /remote/entry", "negative size"),
        ("sh", "no digest available", "invalid output"),
    ],
)
def test_push_malformed_remote_evidence_never_commits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    program: str, malformed: str, message: str,
) -> None:
    """Remote integrity evidence is never guessed or silently accepted."""
    source = tmp_path / "source.bin"
    source.write_bytes(b"verified payload")
    runner = FakeTransferRunner()
    original = runner._ssh

    def corrupt_evidence(argv: tuple[str, ...]) -> ProcessResult:
        if shlex.split(argv[-1])[0] == program:
            return _result(malformed)
        return original(argv)

    monkeypatch.setattr(runner, "_ssh", corrupt_evidence)
    with pytest.raises(SshTransferError, match=message) as caught:
        SshFileTransfer(runner).push(_target(), source, "/remote/entry")
    assert caught.value.committed is False
    assert caught.value.action_attempted is True
    assert "/remote/entry" not in runner.remote_files
    assert not any(".hostops-upload-" in name for name in runner.remote_files)
