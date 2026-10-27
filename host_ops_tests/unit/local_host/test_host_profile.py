from __future__ import annotations

import importlib
from collections import namedtuple

import pytest

from local_agent.host_ops.capabilities.local.host import HostProfile, HostProfileError, HostProfiler
from local_agent.host_ops.core.execution import ExecutionLimits, ProcessResult, ProcessState

profile_module = importlib.import_module("local_agent.host_ops.capabilities.local.host.profile")
DiskUsage = namedtuple("DiskUsage", "total used free")


class FakeRunner:
    def __init__(self, *results: ProcessResult) -> None:
        self.results = results
        self.calls: list[tuple[str, ...]] = []
        self.limits: list[ExecutionLimits | None] = []

    def run(self, argv, *, limits=None):
        result_index = len(self.calls)
        self.calls.append(tuple(argv))
        self.limits.append(limits)
        if result_index >= len(self.results):
            raise AssertionError("unexpected process execution")
        return self.results[result_index]


def _result(
    stdout: str = "",
    *,
    stderr: str = "",
    exit_code: int = 0,
    stdout_truncated: bool = False,
) -> ProcessResult:
    return ProcessResult(
        state=ProcessState.COMPLETED,
        exit_code=exit_code,
        stdout=stdout,
        stderr=stderr,
        duration_seconds=0.01,
        stdout_truncated=stdout_truncated,
    )


def _host_facts(monkeypatch: pytest.MonkeyPatch, *, system: str) -> None:
    monkeypatch.setattr(profile_module.socket, "gethostname", lambda: "builder-01")
    monkeypatch.setattr(profile_module.platform, "system", lambda: system)
    monkeypatch.setattr(profile_module.platform, "release", lambda: "25.0.0")
    monkeypatch.setattr(profile_module.platform, "machine", lambda: "arm64")
    monkeypatch.setattr(profile_module.os, "cpu_count", lambda: 10)
    monkeypatch.setattr(
        profile_module.shutil,
        "disk_usage",
        lambda _path: DiskUsage(total=1_000_000, used=400_000, free=600_000),
    )


def test_host_profile_gpu_field_defaults_to_unavailable() -> None:
    profile = HostProfile(
        hostname="builder-01",
        system="Linux",
        release="6.0",
        architecture="x86_64",
        logical_cpu_count=8,
        memory_total_bytes=8_000_000_000,
        root_total_bytes=1_000_000,
        root_free_bytes=600_000,
    )

    assert profile.gpu_devices is None
    assert profile.as_dict()["gpu_devices"] is None


def test_darwin_profile_uses_pinned_native_tools_for_memory_and_gpu(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _host_facts(monkeypatch, system="Darwin")
    runner = FakeRunner(
        _result("17179869184\n"),
        _result(
            '{"SPDisplaysDataType": ['
            '{"sppci_model": "Apple M1", "_name": "Apple M1"}, '
            '{"_name": "Apple M1"}]}'
        ),
    )

    profile = HostProfiler(runner).inspect()

    assert profile.as_dict() == {
        "hostname": "builder-01",
        "system": "Darwin",
        "release": "25.0.0",
        "architecture": "arm64",
        "logical_cpu_count": 10,
        "memory_total_bytes": 17_179_869_184,
        "gpu_devices": ["Apple M1"],
        "root_total_bytes": 1_000_000,
        "root_free_bytes": 600_000,
    }
    assert runner.calls == [
        ("/usr/sbin/sysctl", "-n", "hw.memsize"),
        (
            "/usr/sbin/system_profiler",
            "-json",
            "-detailLevel",
            "mini",
            "SPDisplaysDataType",
        ),
    ]


def test_darwin_profile_uses_one_whole_operation_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _host_facts(monkeypatch, system="Darwin")
    runner = FakeRunner(
        _result("17179869184\n"),
        _result('{"SPDisplaysDataType": []}'),
    )
    ticks = iter((100.0, 101.0, 104.0))
    monkeypatch.setattr(profile_module.time, "monotonic", lambda: next(ticks))

    HostProfiler(runner).inspect(limits=ExecutionLimits(timeout_seconds=10.0))

    assert [limit.timeout_seconds for limit in runner.limits if limit is not None] == [9.0, 6.0]


def test_darwin_profile_fails_before_second_process_when_budget_is_exhausted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _host_facts(monkeypatch, system="Darwin")
    runner = FakeRunner(_result("17179869184\n"))
    ticks = iter((100.0, 101.0, 110.0))
    monkeypatch.setattr(profile_module.time, "monotonic", lambda: next(ticks))

    with pytest.raises(HostProfileError, match="whole-operation timeout"):
        HostProfiler(runner).inspect(limits=ExecutionLimits(timeout_seconds=10.0))

    assert runner.calls == [("/usr/sbin/sysctl", "-n", "hw.memsize")]


def test_linux_profile_uses_sysconf_without_spawning_process(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _host_facts(monkeypatch, system="Linux")
    runner = FakeRunner(_result("unused"))

    def fake_sysconf(name: str) -> int:
        return {"SC_PHYS_PAGES": 1000, "SC_PAGE_SIZE": 4096}[name]

    monkeypatch.setattr(profile_module.os, "sysconf", fake_sysconf)

    profile = HostProfiler(runner).inspect()

    assert profile.memory_total_bytes == 4_096_000
    assert profile.gpu_devices is None
    assert runner.calls == []


def test_unknown_system_keeps_optional_facts_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _host_facts(monkeypatch, system="FreeBSD")
    runner = FakeRunner(_result("unused"))

    profile = HostProfiler(runner).inspect()

    assert profile.memory_total_bytes is None
    assert profile.gpu_devices is None
    assert runner.calls == []


@pytest.mark.parametrize(
    "gpu_result",
    [
        _result("not-json"),
        _result("{}", exit_code=1),
        _result('{"SPDisplaysDataType": []}', stdout_truncated=True),
        _result("[]"),
        _result("{}"),
    ],
    ids=["malformed-json", "command-failed", "truncated", "wrong-root", "missing-key"],
)
def test_darwin_profile_keeps_gpu_optional_on_unreliable_output(
    monkeypatch: pytest.MonkeyPatch,
    gpu_result: ProcessResult,
) -> None:
    _host_facts(monkeypatch, system="Darwin")
    runner = FakeRunner(_result("17179869184\n"), gpu_result)

    profile = HostProfiler(runner).inspect()

    assert profile.memory_total_bytes == 17_179_869_184
    assert profile.gpu_devices is None


def test_darwin_profile_reports_empty_gpu_inventory(monkeypatch: pytest.MonkeyPatch) -> None:
    _host_facts(monkeypatch, system="Darwin")
    runner = FakeRunner(
        _result("17179869184\n"),
        _result('{"SPDisplaysDataType": []}'),
    )

    profile = HostProfiler(runner).inspect()

    assert profile.gpu_devices == ()


def test_darwin_profile_rejects_invalid_memory_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _host_facts(monkeypatch, system="Darwin")

    with pytest.raises(HostProfileError, match="invalid integer"):
        HostProfiler(FakeRunner(_result("not-a-number\n"))).inspect()


def test_darwin_profile_rejects_truncated_sysctl_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _host_facts(monkeypatch, system="Darwin")

    with pytest.raises(HostProfileError, match="truncated"):
        HostProfiler(FakeRunner(_result("123", stdout_truncated=True))).inspect()


def test_profile_reports_root_filesystem_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    _host_facts(monkeypatch, system="Darwin")

    def fail(_path: str):
        raise OSError("unavailable")

    monkeypatch.setattr(profile_module.shutil, "disk_usage", fail)

    with pytest.raises(HostProfileError, match="root filesystem"):
        HostProfiler(FakeRunner(_result("123\n"))).inspect()
