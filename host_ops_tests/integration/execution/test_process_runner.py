from __future__ import annotations

import os
import signal
import sys
from pathlib import Path

import pytest

from local_agent.host_ops.core.execution import ExecutionLimits, ProcessRunner, ProcessState

_VALID_LEASE_DIGEST = "0" * 64


def test_process_runner_captures_exit_and_streams() -> None:
    result = ProcessRunner().run(
        (
            sys.executable,
            "-c",
            "import sys; print('out'); print('err', file=sys.stderr); raise SystemExit(7)",
        )
    )
    assert result.state is ProcessState.COMPLETED
    assert result.exit_code == 7
    assert result.stdout == "out\n"
    assert result.stderr == "err\n"
    assert not result.ok


def test_process_runner_bounds_stdout_while_draining_pipe() -> None:
    result = ProcessRunner().run(
        (sys.executable, "-c", "print('x' * 10000)"),
        limits=ExecutionLimits(max_stdout_bytes=32),
    )
    assert result.exit_code == 0
    assert result.stdout == "x" * 32
    assert result.stdout_truncated


def test_process_runner_times_out_and_terminates_process() -> None:
    result = ProcessRunner().run(
        (sys.executable, "-c", "import time; time.sleep(5)"),
        limits=ExecutionLimits(timeout_seconds=0.15, terminate_grace_seconds=0.1),
    )
    assert result.state is ProcessState.TIMED_OUT
    assert result.duration_seconds < 2
    assert not result.ok
    assert result.error is not None


@pytest.mark.skipif(os.name != "posix", reason="signal escalation is POSIX-specific")
def test_process_runner_escalates_timeout_when_sigterm_is_ignored() -> None:
    child_code = (
        "import signal, time; "
        "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        "print('ready', flush=True); "
        "time.sleep(5)"
    )
    result = ProcessRunner().run(
        (sys.executable, "-c", child_code),
        limits=ExecutionLimits(timeout_seconds=2.0, terminate_grace_seconds=0.1),
    )
    assert result.state is ProcessState.TIMED_OUT
    assert result.exit_code == -signal.SIGKILL
    assert result.stdout == "ready\n"
    assert result.duration_seconds < 4
    assert not result.ok


@pytest.mark.skipif(os.name != "posix", reason="process-group cleanup is POSIX-specific")
def test_process_runner_cleans_descendants_holding_output_pipe() -> None:
    parent_code = (
        "import subprocess, sys; "
        "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(5)']); "
        "print('parent done')"
    )
    result = ProcessRunner().run(
        (sys.executable, "-c", parent_code),
        limits=ExecutionLimits(
            timeout_seconds=2,
            terminate_grace_seconds=0.1,
            pipe_drain_seconds=0.05,
        ),
    )
    assert result.state is ProcessState.LINGERING_DESCENDANTS
    assert result.stdout == "parent done\n"
    assert result.duration_seconds < 3
    assert not result.ok


@pytest.mark.skipif(os.name != "posix", reason="process-group cleanup is POSIX-specific")
def test_process_runner_cleans_descendants_that_close_output_pipes() -> None:
    parent_code = (
        "import subprocess, sys; "
        "subprocess.Popen("
        "[sys.executable, '-c', 'import time; time.sleep(5)'], "
        "stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL); "
        "print('parent done', flush=True)"
    )
    # Keep the hard deadline beyond transient sandbox scheduler stalls.
    # The assertion still requires descendant cleanup, not a normal exit.
    result = ProcessRunner().run(
        (sys.executable, "-c", parent_code),
        limits=ExecutionLimits(
            timeout_seconds=8,
            terminate_grace_seconds=0.1,
            pipe_drain_seconds=0.05,
        ),
    )
    assert result.state is ProcessState.LINGERING_DESCENDANTS
    assert result.stdout == "parent done\n"
    assert result.duration_seconds < 8
    assert not result.ok


@pytest.mark.skipif(os.name != "posix", reason="lease descriptor handling is POSIX-specific")
def test_process_runner_keeps_outer_group_without_leaking_local_agent_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository_path = tmp_path / "repository.lock"
    resource_path = tmp_path / "resource.lock"
    with (
        repository_path.open("a+") as repository_lease,
        resource_path.open("a+") as resource_lease,
    ):
        repository_fd = repository_lease.fileno()
        resource_fd = resource_lease.fileno()
        monkeypatch.setenv("LOCAL_AGENT_LEASE_FDS", str(repository_fd))
        monkeypatch.setenv("LOCAL_AGENT_RESOURCE_LEASE_FDS", str(resource_fd))
        monkeypatch.setenv("LOCAL_AGENT_LEASE_KEYS_DIGEST", _VALID_LEASE_DIGEST)
        parent_group = os.getpgrp()
        code = (
            "import os; "
            f"fds=({repository_fd},{resource_fd}); "
            "closed=[]; "
            "\nfor fd in fds:\n"
            " try:\n  os.fstat(fd); closed.append(False)\n"
            " except OSError:\n  closed.append(True)\n"
            "names=('LOCAL_AGENT_LEASE_FDS','LOCAL_AGENT_RESOURCE_LEASE_FDS',"
            "'LOCAL_AGENT_LEASE_KEYS_DIGEST'); "
            "leaked=any(name in os.environ for name in names); "
            "print(f'{int(all(closed))}:{int(leaked)}:{os.getpgrp()}', flush=True)"
        )
        result = ProcessRunner().run((sys.executable, "-c", code))
    assert result.ok
    assert result.stdout.strip() == f"1:0:{parent_group}"


@pytest.mark.skipif(os.name != "posix", reason="lease descriptor handling is POSIX-specific")
@pytest.mark.parametrize(
    "name",
    [
        "LOCAL_AGENT_LEASE_FDS",
        "LOCAL_AGENT_RESOURCE_LEASE_FDS",
        "LOCAL_AGENT_LEASE_KEYS_DIGEST",
    ],
)
def test_process_runner_rejects_overriding_local_agent_lease_state(name: str) -> None:
    with pytest.raises(ValueError, match="lease state"):
        ProcessRunner().run(
            (sys.executable, "-c", "pass"),
            env_overrides={name: ""},
        )


@pytest.mark.skipif(os.name != "posix", reason="lease descriptor handling is POSIX-specific")
def test_process_runner_rejects_repository_lease_without_digest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with (tmp_path / "repository.lock").open("a+") as lease:
        monkeypatch.setenv("LOCAL_AGENT_LEASE_FDS", str(lease.fileno()))
        monkeypatch.delenv("LOCAL_AGENT_LEASE_KEYS_DIGEST", raising=False)
        monkeypatch.delenv("LOCAL_AGENT_RESOURCE_LEASE_FDS", raising=False)
        with pytest.raises(ValueError, match="without repository lease digest"):
            ProcessRunner().run((sys.executable, "-c", "pass"))


@pytest.mark.skipif(os.name != "posix", reason="lease descriptor handling is POSIX-specific")
def test_process_runner_accepts_host_resource_lease_without_repository_lease(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with (tmp_path / "resource.lock").open("a+") as lease:
        monkeypatch.delenv("LOCAL_AGENT_LEASE_FDS", raising=False)
        monkeypatch.setenv("LOCAL_AGENT_RESOURCE_LEASE_FDS", str(lease.fileno()))
        monkeypatch.delenv("LOCAL_AGENT_LEASE_KEYS_DIGEST", raising=False)
        parent_group = os.getpgrp()
        result = ProcessRunner().run((sys.executable, "-c", "import os; print(os.getpgrp())"))
        assert result.ok
        assert result.stdout.strip() == str(parent_group)
        os.fstat(lease.fileno())  # The host wrapper retains its lease throughout execution.


def test_process_runner_rejects_host_resource_state_with_repository_digest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    with (tmp_path / "resource.lock").open("a+") as lease:
        monkeypatch.delenv("LOCAL_AGENT_LEASE_FDS", raising=False)
        monkeypatch.setenv("LOCAL_AGENT_RESOURCE_LEASE_FDS", str(lease.fileno()))
        monkeypatch.setenv("LOCAL_AGENT_LEASE_KEYS_DIGEST", _VALID_LEASE_DIGEST)
        with pytest.raises(ValueError, match="without repository lease"):
            ProcessRunner().run((sys.executable, "-c", "pass"))


def test_process_runner_rejects_digest_without_repository_lease(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("LOCAL_AGENT_LEASE_FDS", raising=False)
    monkeypatch.delenv("LOCAL_AGENT_RESOURCE_LEASE_FDS", raising=False)
    monkeypatch.setenv("LOCAL_AGENT_LEASE_KEYS_DIGEST", _VALID_LEASE_DIGEST)
    with pytest.raises(ValueError, match="without repository lease"):
        ProcessRunner().run((sys.executable, "-c", "pass"))


@pytest.mark.skipif(os.name != "posix", reason="lease descriptor handling is POSIX-specific")
def test_process_runner_rejects_malformed_local_agent_lease_digest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with (tmp_path / "repository.lock").open("a+") as lease:
        monkeypatch.setenv("LOCAL_AGENT_LEASE_FDS", str(lease.fileno()))
        monkeypatch.setenv("LOCAL_AGENT_LEASE_KEYS_DIGEST", "not-a-digest")
        monkeypatch.delenv("LOCAL_AGENT_RESOURCE_LEASE_FDS", raising=False)
        with pytest.raises(ValueError, match="LOCAL_AGENT_LEASE_KEYS_DIGEST"):
            ProcessRunner().run((sys.executable, "-c", "pass"))


def test_process_runner_rejects_malformed_local_agent_lease_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LOCAL_AGENT_LEASE_FDS", "not-a-descriptor")
    monkeypatch.setenv("LOCAL_AGENT_LEASE_KEYS_DIGEST", _VALID_LEASE_DIGEST)
    with pytest.raises(ValueError, match="LOCAL_AGENT_LEASE_FDS"):
        ProcessRunner().run((sys.executable, "-c", "pass"))


@pytest.mark.skipif(os.name != "posix", reason="lease descriptor handling is POSIX-specific")
def test_process_runner_rejects_duplicate_repository_lease_descriptors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with (tmp_path / "repository.lock").open("a+") as lease:
        descriptor = lease.fileno()
        monkeypatch.setenv("LOCAL_AGENT_LEASE_FDS", f"{descriptor},{descriptor}")
        monkeypatch.setenv("LOCAL_AGENT_LEASE_KEYS_DIGEST", _VALID_LEASE_DIGEST)
        monkeypatch.delenv("LOCAL_AGENT_RESOURCE_LEASE_FDS", raising=False)
        with pytest.raises(ValueError, match="LOCAL_AGENT_LEASE_FDS"):
            ProcessRunner().run((sys.executable, "-c", "pass"))


@pytest.mark.skipif(os.name != "posix", reason="lease descriptor handling is POSIX-specific")
def test_process_runner_rejects_overlapping_repository_and_resource_descriptors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with (tmp_path / "repository.lock").open("a+") as lease:
        descriptor = lease.fileno()
        monkeypatch.setenv("LOCAL_AGENT_LEASE_FDS", str(descriptor))
        monkeypatch.setenv("LOCAL_AGENT_RESOURCE_LEASE_FDS", str(descriptor))
        monkeypatch.setenv("LOCAL_AGENT_LEASE_KEYS_DIGEST", _VALID_LEASE_DIGEST)
        with pytest.raises(ValueError, match="descriptors overlap"):
            ProcessRunner().run((sys.executable, "-c", "pass"))


@pytest.mark.skipif(os.name != "posix", reason="lease descriptor handling is POSIX-specific")
def test_process_runner_accepts_forwarded_closed_lease_metadata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lease = (tmp_path / "repository.lock").open("a+")
    descriptor = lease.fileno()
    lease.close()
    monkeypatch.setenv("LOCAL_AGENT_LEASE_FDS", str(descriptor))
    monkeypatch.setenv("LOCAL_AGENT_LEASE_KEYS_DIGEST", _VALID_LEASE_DIGEST)
    monkeypatch.delenv("LOCAL_AGENT_RESOURCE_LEASE_FDS", raising=False)
    parent_group = os.getpgrp()
    code = (
        "import os; "
        "names=('LOCAL_AGENT_LEASE_FDS','LOCAL_AGENT_RESOURCE_LEASE_FDS',"
        "'LOCAL_AGENT_LEASE_KEYS_DIGEST'); "
        "print(f'{int(any(name in os.environ for name in names))}:{os.getpgrp()}')"
    )
    result = ProcessRunner().run((sys.executable, "-c", code))
    assert result.ok
    assert result.stdout.strip() == f"0:{parent_group}"


@pytest.mark.skipif(os.name != "posix", reason="lease descriptor handling is POSIX-specific")
def test_process_runner_rejects_partially_inherited_lease_descriptors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with (tmp_path / "repository.lock").open("a+") as repository_lease:
        resource_lease = (tmp_path / "resource.lock").open("a+")
        resource_descriptor = resource_lease.fileno()
        resource_lease.close()
        monkeypatch.setenv("LOCAL_AGENT_LEASE_FDS", str(repository_lease.fileno()))
        monkeypatch.setenv("LOCAL_AGENT_RESOURCE_LEASE_FDS", str(resource_descriptor))
        monkeypatch.setenv("LOCAL_AGENT_LEASE_KEYS_DIGEST", _VALID_LEASE_DIGEST)
        with pytest.raises(ValueError, match="partially inherited"):
            ProcessRunner().run((sys.executable, "-c", "pass"))


def test_process_result_does_not_copy_argv_into_evidence() -> None:
    secret_argument = "super-secret-argv-value"
    result = ProcessRunner().run((sys.executable, "-c", "pass", secret_argument))
    assert result.ok
    assert "argv" not in result.as_dict()
    assert secret_argument not in repr(result)


def test_process_runner_normalizes_spawn_failure() -> None:
    result = ProcessRunner().run(("host-ops-command-that-does-not-exist-27f19",))
    assert result.state is ProcessState.SPAWN_FAILED
    assert result.exit_code is None
    assert result.error is not None
    assert not result.ok


def test_process_runner_applies_environment_overrides() -> None:
    result = ProcessRunner().run(
        (sys.executable, "-c", "import os; print(os.environ['HOST_OPS_TEST_VALUE'])"),
        env_overrides={"HOST_OPS_TEST_VALUE": "expected"},
    )
    assert result.ok
    assert result.stdout == "expected\n"


@pytest.mark.parametrize("name", ["", "BAD=NAME", "BAD\x00NAME"])
def test_process_runner_rejects_invalid_environment_override_names(name: str) -> None:
    with pytest.raises(ValueError, match="environment override names"):
        ProcessRunner().run(
            (sys.executable, "-c", "pass"),
            env_overrides={name: "value"},
        )


def test_process_runner_rejects_nul_in_environment_override_value() -> None:
    with pytest.raises(ValueError, match="must not contain NUL"):
        ProcessRunner().run(
            (sys.executable, "-c", "pass"),
            env_overrides={"HOST_OPS_TEST_VALUE": "bad\x00value"},
        )


def test_process_runner_respects_cwd(tmp_path: Path) -> None:
    result = ProcessRunner().run(
        (sys.executable, "-c", "from pathlib import Path; print(Path.cwd())"),
        cwd=tmp_path,
    )
    assert result.ok
    assert result.stdout.strip() == str(tmp_path)


def test_process_runner_preserves_empty_non_program_arguments() -> None:
    result = ProcessRunner().run(
        (sys.executable, "-c", "import sys; print(repr(sys.argv[1]))", ""),
    )
    assert result.ok
    assert result.stdout == "''\n"


def test_process_runner_rejects_empty_program_name() -> None:
    with pytest.raises(ValueError, match=r"argv\[0\]"):
        ProcessRunner().run(("", "argument"))


def test_process_runner_rejects_nul_bytes() -> None:
    with pytest.raises(ValueError, match="NUL"):
        ProcessRunner().run((sys.executable, "bad\x00argument"))


def test_process_runner_rejects_single_string_argv() -> None:
    with pytest.raises(ValueError, match="single string"):
        ProcessRunner().run("echo hello")
