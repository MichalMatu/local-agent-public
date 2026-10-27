from __future__ import annotations

import pytest

import local_agent.host_ops.capabilities.local.tools.inspection as inspection_module
from local_agent.host_ops.capabilities.local.tools import ToolInspectionError, ToolInspector
from local_agent.host_ops.core.execution import ExecutionLimits, ProcessResult, ProcessState


def _result(
    *,
    state: ProcessState = ProcessState.COMPLETED,
    exit_code: int | None = 0,
    stdout: str = "",
    stderr: str = "",
    stdout_truncated: bool = False,
    stderr_truncated: bool = False,
    error: str | None = None,
) -> ProcessResult:
    return ProcessResult(
        state=state,
        exit_code=exit_code,
        stdout=stdout,
        stderr=stderr,
        duration_seconds=0.01,
        stdout_truncated=stdout_truncated,
        stderr_truncated=stderr_truncated,
        error=error,
    )


class FakeRunner:
    def __init__(self, results: list[ProcessResult]) -> None:
        self._results = list(results)
        self.calls: list[tuple[tuple[str, ...], ExecutionLimits | None]] = []

    def run(self, argv, *, cwd=None, env_overrides=None, limits=None):
        self.calls.append((tuple(argv), limits))
        return self._results.pop(0)


def test_inspect_reports_resolved_path_and_first_version_line(monkeypatch) -> None:
    runner = FakeRunner([_result(stdout="Python 3.13.9\nsecond line\n")])
    monkeypatch.setattr(
        inspection_module.shutil,
        "which",
        lambda name: "/opt/bin/python3",
    )

    results = ToolInspector(runner=runner).inspect(["python3"])

    assert results[0].as_dict() == {
        "name": "python3",
        "present": True,
        "resolved_path": "/opt/bin/python3",
        "version": "Python 3.13.9",
        "version_exit_code": 0,
        "error": None,
    }
    assert runner.calls[0][0] == ("/opt/bin/python3", "--version")
    assert runner.calls[0][1] is not None
    assert runner.calls[0][1].timeout_seconds == 5.0
    assert runner.calls[0][1].max_stdout_bytes == 8192


def test_inspect_openssh_uses_supported_version_flag_and_stderr(monkeypatch) -> None:
    runner = FakeRunner([_result(stderr="OpenSSH_10.0p1, LibreSSL 3.3.6\n")])
    monkeypatch.setattr(inspection_module.shutil, "which", lambda _: "/usr/bin/ssh")

    result = ToolInspector(runner=runner).inspect(["ssh"])[0]

    assert runner.calls[0][0] == ("/usr/bin/ssh", "-V")
    assert result.present is True
    assert result.version == "OpenSSH_10.0p1, LibreSSL 3.3.6"
    assert result.version_exit_code == 0
    assert result.error is None


def test_inspect_missing_tool_does_not_spawn(monkeypatch) -> None:
    runner = FakeRunner([])
    monkeypatch.setattr(inspection_module.shutil, "which", lambda name: None)

    results = ToolInspector(runner=runner).inspect(["missing-tool"])

    assert results[0].present is False
    assert results[0].resolved_path is None
    assert results[0].version is None
    assert runner.calls == []


def test_inspect_uses_stderr_version_and_preserves_failed_probe(monkeypatch) -> None:
    runner = FakeRunner([_result(exit_code=2, stderr="tool 1.2.3\n")])
    monkeypatch.setattr(
        inspection_module.shutil,
        "which",
        lambda name: "/opt/bin/tool",
    )

    result = ToolInspector(runner=runner).inspect(["tool"])[0]

    assert result.version == "tool 1.2.3"
    assert result.version_exit_code == 2
    assert result.error == "version probe exited with code 2"


def test_inspect_reports_timeout_and_truncation(monkeypatch) -> None:
    monkeypatch.setattr(
        inspection_module.shutil,
        "which",
        lambda name: f"/opt/bin/{name}",
    )
    runner = FakeRunner(
        [
            _result(
                state=ProcessState.TIMED_OUT,
                exit_code=None,
                error="process timed out",
            ),
            _result(stdout="tool 1.0\n", stdout_truncated=True),
        ]
    )

    results = ToolInspector(runner=runner).inspect(["slow", "verbose"])

    assert results[0].error == "process timed out"
    assert results[1].version == "tool 1.0"
    assert results[1].error == "version output was truncated"


def test_inspect_deduplicates_names_and_uses_supplied_limits(monkeypatch) -> None:
    monkeypatch.setattr(
        inspection_module.shutil,
        "which",
        lambda name: "/opt/bin/git",
    )
    runner = FakeRunner([_result(stdout="git version 2.51.0\n")])
    limits = ExecutionLimits(
        timeout_seconds=2.0,
        max_stdout_bytes=1024,
        max_stderr_bytes=1024,
    )

    results = ToolInspector(runner=runner).inspect(["git", "git"], limits=limits)

    assert len(results) == 1
    assert runner.calls == [(("/opt/bin/git", "--version"), limits)]


@pytest.mark.parametrize("name", ["", "../git", "/bin/git", "tool name", "a" * 129])
def test_inspect_rejects_invalid_tool_names(name: str) -> None:
    with pytest.raises(ToolInspectionError, match="invalid tool name"):
        ToolInspector(runner=FakeRunner([])).inspect([name])


def test_inspect_requires_names_and_caps_batch_size() -> None:
    inspector = ToolInspector(runner=FakeRunner([]))

    with pytest.raises(ToolInspectionError, match="at least one"):
        inspector.inspect([])
    with pytest.raises(ToolInspectionError, match="at most 64"):
        inspector.inspect([f"tool-{index}" for index in range(65)])
