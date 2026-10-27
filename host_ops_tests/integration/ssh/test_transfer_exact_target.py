"""Exercise the actual ln/mv utility namespace semantics at transfer commit.

The transport is deterministic and local; no SSH host or installed service is touched.
The barrier is the utility invocation, not a sleep or production fault flag.
"""

from __future__ import annotations

import hashlib
import shlex
import subprocess
from pathlib import Path

import pytest

from local_agent.host_ops.capabilities.remote.ssh import (
    SYSTEM_SCP_EXECUTABLE,
    SshFileTransfer,
    SshTransferError,
)
from local_agent.host_ops.core.config import HostTarget
from local_agent.host_ops.core.execution import ProcessResult, ProcessState


def _result(
    stdout: str = "", *, exit_code: int | None = 0,
    stderr: str = "", state: ProcessState = ProcessState.COMPLETED,
) -> ProcessResult:
    return ProcessResult(
        state=state, exit_code=exit_code, stdout=stdout,
        stderr=stderr, duration_seconds=0.001,
    )


def _target() -> HostTarget:
    return HostTarget(
        alias="native-fixture", host="example.test", port=22,
        user="tester", identity_file="/tmp/unused-test-identity",
    )


class _NativeUtilitiesTransport:
    def __init__(
        self, tmp_path: Path, *, mutate: str | None = None,
        deny_links: bool = False, lost_commit_reply: bool = False,
    ) -> None:
        self.root = tmp_path
        self.remote = tmp_path / "remote"
        self.remote.mkdir()
        self.external = tmp_path / "external"
        self.external.mkdir()
        self.mutate = mutate
        self.deny_links = deny_links
        self.lost_commit_reply = lost_commit_reply
        self.commit_commands = 0
        self.did_mutate = False

    def _path(self, remote: str) -> Path:
        assert remote == "/remote" or remote.startswith("/remote/")
        if remote == "/remote":
            return self.remote
        return self.remote / remote.removeprefix("/remote/")

    def run(self, command, *, limits=None, **_kwargs) -> ProcessResult:
        argv = tuple(command)
        if argv[0] == SYSTEM_SCP_EXECUTABLE:
            # SCP push has local-source and remote user@host:/remote/... operands.
            source = Path(argv[-2])
            remote_path = "/"+argv[-1].split(":/", 1)[1]
            self._path(remote_path).write_bytes(source.read_bytes())
            return _result()
        remote = tuple(shlex.split(argv[-1]))
        program = remote[0]
        if program == "test":
            flag, name = remote[1:]
            path = self._path(name)
            matched = {
                "-d": path.is_dir(),
                "-e": path.exists(),
                "-f": path.is_file(),
                "-L": path.is_symlink(),
            }[flag]
            return _result(exit_code=0 if matched else 1)
        if program == "wc":
            path = self._path(remote[-1])
            if not path.is_file():
                return _result(exit_code=1, stderr="not a file")
            return _result(f"{path.stat().st_size} {remote[-1]}\n")
        if program == "sh":
            path = self._path(remote[-1])
            if not path.is_file():
                return _result(exit_code=1, stderr="not a file")
            return _result(hashlib.sha256(path.read_bytes()).hexdigest() + "\n")
        if program == "rm":
            self._path(remote[-1]).unlink(missing_ok=True)
            return _result()
        if program in ("ln", "mv"):
            self.commit_commands += 1
            if not self.did_mutate and self.mutate is not None:
                self.did_mutate = True
                destination = self.remote / "output.bin"
                if self.mutate == "directory":
                    destination.mkdir()
                elif self.mutate == "symlink":
                    destination.symlink_to(self.external, target_is_directory=True)
                elif self.mutate == "file":
                    destination.write_bytes(b"other-writer")
                else:
                    raise AssertionError(self.mutate)
            if program == "ln" and self.deny_links:
                return _result(exit_code=1, stderr="hard links not permitted")
            utility_argv = [
                self._path(item).as_posix() if item.startswith("/remote/") else item
                for item in remote
            ]
            completed = subprocess.run(
                utility_argv, capture_output=True, text=True, check=False,
            )
            if completed.returncode == 0 and self.lost_commit_reply:
                return _result(
                    exit_code=None, stderr="connection lost after commit",
                    state=ProcessState.TIMED_OUT,
                )
            return _result(
                completed.stdout, exit_code=completed.returncode,
                stderr=completed.stderr,
            )
        raise AssertionError(f"unexpected remote command: {remote!r}")


@pytest.mark.parametrize("replace", [False, True])
@pytest.mark.parametrize("appearing", ["directory", "symlink"])
def test_commit_never_writes_inside_new_destination(
    tmp_path: Path, replace: bool, appearing: str,
) -> None:
    source = tmp_path / "source.bin"
    source.write_bytes(b"verified payload")
    transport = _NativeUtilitiesTransport(tmp_path, mutate=appearing)

    outcome = None
    try:
        outcome = SshFileTransfer(transport).push(
            _target(), source, "/remote/output.bin", replace=replace,
        )
    except SshTransferError:
        pass

    destination = transport.remote / "output.bin"
    if replace and appearing == "symlink" and outcome is not None:
        # Exact-entry rename may atomically replace the link itself; it must
        # never follow it and write into the external directory.
        assert destination.is_file()
        assert not destination.is_symlink()
        assert destination.read_bytes() == source.read_bytes()
    else:
        assert outcome is None
        assert destination.is_dir()
        assert not list(destination.glob(".hostops-upload-*.tmp"))
    assert not list(transport.external.iterdir())
    assert not list(transport.remote.glob(".hostops-upload-*.tmp"))
    assert transport.commit_commands == 1


def test_racing_regular_file_keeps_original_no_clobber(tmp_path: Path) -> None:
    source = tmp_path / "source.bin"
    source.write_bytes(b"new")
    transport = _NativeUtilitiesTransport(tmp_path, mutate="file")
    with pytest.raises(SshTransferError):
        SshFileTransfer(transport).push(_target(), source, "/remote/output.bin")
    assert (transport.remote / "output.bin").read_bytes() == b"other-writer"
    assert not list(transport.remote.glob(".hostops-upload-*.tmp"))


def test_denied_hardlink_does_not_fall_back_to_unsafe_move(tmp_path: Path) -> None:
    source = tmp_path / "source.bin"
    source.write_bytes(b"new")
    transport = _NativeUtilitiesTransport(tmp_path, deny_links=True)
    with pytest.raises(SshTransferError):
        SshFileTransfer(transport).push(_target(), source, "/remote/output.bin")
    assert not (transport.remote / "output.bin").exists()
    assert transport.commit_commands == 1
    assert not list(transport.remote.glob(".hostops-upload-*.tmp"))


def test_lost_commit_response_reports_unknown_not_definitely_false(tmp_path: Path) -> None:
    if subprocess.run(("ln", "--version"), capture_output=True, check=False).returncode != 0:
        pytest.skip("GNU exact-target hard-link utility not installed")
    source = tmp_path / "source.bin"
    source.write_bytes(b"verified payload")
    transport = _NativeUtilitiesTransport(tmp_path, lost_commit_reply=True)
    with pytest.raises(SshTransferError) as caught:
        SshFileTransfer(transport).push(_target(), source, "/remote/output.bin")
    assert (transport.remote / "output.bin").read_bytes() == b"verified payload"
    assert caught.value.action_attempted
    assert caught.value.commit_uncertain
    assert caught.value.committed is False
    assert transport.commit_commands == 1


def test_exact_target_normal_push_on_supported_gnu_link(tmp_path: Path) -> None:
    # This positive case requires GNU ln -T; BSD/macOS must refuse unsafe mode.
    probe = subprocess.run(("ln", "--version"), capture_output=True, check=False)
    if probe.returncode != 0:
        pytest.skip("GNU exact-target hard-link utility not installed")
    source = tmp_path / "source.bin"
    source.write_bytes(bytes(range(256)))
    transport = _NativeUtilitiesTransport(tmp_path)
    result = SshFileTransfer(transport).push(_target(), source, "/remote/output.bin")
    assert result.sha256 == hashlib.sha256(source.read_bytes()).hexdigest()
    assert (transport.remote / "output.bin").read_bytes() == source.read_bytes()
    assert transport.commit_commands == 1
    assert not list(transport.remote.glob(".hostops-upload-*.tmp"))



def test_lost_replace_response_reports_unknown_and_does_not_replay(
    tmp_path: Path,
) -> None:
    # A rename may have completed before SSH loses its response. Never
    # report committed=False as proof that destination bytes did not change.
    probe = subprocess.run(("mv", "--version"), capture_output=True, check=False)
    if probe.returncode != 0:
        pytest.skip("GNU exact-target rename utility not installed")
    source = tmp_path / "source.bin"
    source.write_bytes(b"new verified payload")
    transport = _NativeUtilitiesTransport(tmp_path, lost_commit_reply=True)
    with pytest.raises(SshTransferError) as caught:
        SshFileTransfer(transport).push(
            _target(), source, "/remote/output.bin", replace=True,
        )
    assert (transport.remote / "output.bin").read_bytes() == b"new verified payload"
    assert caught.value.code == "ssh_commit_uncertain"
    assert caught.value.phase == "remote_commit"
    assert caught.value.action_attempted
    assert caught.value.commit_uncertain
    assert not caught.value.committed
    assert transport.commit_commands == 1
    assert not list(transport.remote.glob(".hostops-upload-*.tmp"))
