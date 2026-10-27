"""Offline host capacity observations: platform fallbacks, bounds and read-only CLI."""
from __future__ import annotations

import importlib
import io
import json
from collections import namedtuple

import pytest

from local_agent.host_ops.capabilities.local.host import HostCapacityProfiler, HostProfileError
from local_agent.host_ops.core.execution import ExecutionLimits, ProcessResult, ProcessState

capacity = importlib.import_module("local_agent.host_ops.capabilities.local.host.capacity")
host_cmd = importlib.import_module("local_agent.host_ops.cli.commands.host")
cli_main = importlib.import_module("local_agent.host_ops.cli.main")
DiskUsage = namedtuple("DiskUsage", "total used free")


def _result(stdout: str, *, code: int = 0, truncated: bool = False) -> ProcessResult:
    return ProcessResult(
        state=ProcessState.COMPLETED,
        exit_code=code,
        stdout=stdout,
        stderr="",
        duration_seconds=0.01,
        stdout_truncated=truncated,
    )


class FakeRunner:
    def __init__(self, result: ProcessResult) -> None:
        self.result = result
        self.calls: list[tuple[str, ...]] = []
        self.limits: list[ExecutionLimits] = []

    def run(self, argv, *, limits):
        self.calls.append(tuple(argv))
        self.limits.append(limits)
        return self.result


@pytest.fixture
def facts(monkeypatch):
    monkeypatch.setattr(capacity.shutil, "disk_usage", lambda _: DiskUsage(10_000, 7_000, 3_000))
    monkeypatch.setattr(capacity.os, "cpu_count", lambda: 8)
    monkeypatch.setattr(capacity.os, "getloadavg", lambda: (1.0, 2.5, 3.0))


def test_linux_memavailable_is_bounded_and_not_memfree(monkeypatch, facts):
    monkeypatch.setattr(capacity.platform, "system", lambda: "Linux")
    monkeypatch.setattr(
        capacity, "open",
        lambda *_args, **_kw: io.BytesIO(b"MemFree: 999999 kB\nMemAvailable: 4096 kB\n"),
        raising=False,
    )
    runner = FakeRunner(_result("unused"))
    snap = HostCapacityProfiler(runner).inspect()
    assert snap.memory_available_bytes == 4_194_304
    assert snap.memory_availability_kind == "linux_memavailable"
    assert snap.load_average_1m == 1.0
    assert snap.load_average_5m == 2.5
    assert snap.load_average_15m == 3.0
    assert snap.logical_cpu_count == 8
    assert snap.root_total_bytes == 10_000
    assert snap.root_free_bytes == 3_000
    assert snap.as_dict()["observed_at_utc"].endswith("+00:00")
    assert runner.calls == []


@pytest.mark.parametrize("data", [
    b"MemFree: 42 kB\n", b"MemAvailable: -1 kB", b"MemAvailable: surprise kB",
    b"MemAvailable: 184467440737095516161 kB", b"x" * 17_000,
])
def test_linux_bad_or_oversize_meminfo_returns_unknown(monkeypatch, facts, data):
    monkeypatch.setattr(capacity.platform, "system", lambda: "Linux")
    monkeypatch.setattr(capacity, "open", lambda *_a, **_kw: io.BytesIO(data), raising=False)
    snap = HostCapacityProfiler(FakeRunner(_result(""))).inspect()
    assert snap.memory_available_bytes is None
    assert snap.memory_availability_kind is None


def test_linux_proc_unavailable_is_unknown_not_free_memory(monkeypatch, facts):
    monkeypatch.setattr(capacity.platform, "system", lambda: "Linux")

    def fail(*_args, **_kwargs):
        raise OSError("not mounted")

    monkeypatch.setattr(capacity, "open", fail, raising=False)
    assert HostCapacityProfiler().inspect().memory_available_bytes is None


def test_macos_vm_stat_estimate_has_honest_source_and_one_bounded_probe(monkeypatch, facts):
    monkeypatch.setattr(capacity.platform, "system", lambda: "Darwin")
    runner = FakeRunner(_result(
        "Mach Virtual Memory Statistics: (page size of 16384 bytes)\n"
        "Pages free: 100.\nPages inactive: 300.\nPages speculative: 25.\n"
        "Pages purgeable: 90.\n"
    ))
    limits = ExecutionLimits(timeout_seconds=3.0, max_stdout_bytes=4096)
    snapshot = HostCapacityProfiler(runner).inspect(limits=limits)
    assert snapshot.memory_available_bytes == 425 * 16_384
    assert snapshot.memory_availability_kind == "macos_vm_stat_estimate"
    assert runner.calls == [("/usr/bin/vm_stat",)]
    assert runner.limits == [limits]


@pytest.mark.parametrize("result", [
    _result("broken"), _result("Mach Virtual Memory Statistics: (page size of 16384 bytes)\nPages free: 10.\n"),
    _result("Mach Virtual Memory Statistics: (page size of 16384 bytes)\nPages free: 10.\nPages inactive: 20.\n", truncated=True),
    _result("Mach Virtual Memory Statistics: (page size of 16384 bytes)\nPages free: 10.\nPages inactive: 20.\n", code=1),
    _result("Mach Virtual Memory Statistics: (page size of 16384 bytes)\nPages free: 10.\nPages free: 12.\nPages inactive: 20.\n"),
])
def test_macos_invalid_or_failed_probe_returns_unknown(monkeypatch, facts, result):
    monkeypatch.setattr(capacity.platform, "system", lambda: "Darwin")
    snap = HostCapacityProfiler(FakeRunner(result)).inspect()
    assert snap.memory_available_bytes is None
    assert snap.memory_availability_kind is None


def test_unknown_platform_and_load_failure_do_not_imply_free_capacity(monkeypatch, facts):
    monkeypatch.setattr(capacity.platform, "system", lambda: "Windows")
    monkeypatch.setattr(capacity.os, "getloadavg", lambda: (_ for _ in ()).throw(OSError()))
    snap = HostCapacityProfiler().inspect()
    assert snap.memory_available_bytes is None
    assert snap.load_average_1m is None
    assert snap.load_average_5m is None
    assert snap.load_average_15m is None


def test_root_disk_failure_is_explicit(monkeypatch):
    monkeypatch.setattr(
        capacity.shutil, "disk_usage",
        lambda _: (_ for _ in ()).throw(OSError("unavailable")),
    )
    with pytest.raises(HostProfileError, match="root filesystem"):
        HostCapacityProfiler().inspect()


def test_invalid_timeout_is_rejected_before_probe(facts):
    with pytest.raises(ValueError, match="capacity timeout"):
        HostCapacityProfiler().inspect(limits=ExecutionLimits(timeout_seconds=3601))


def test_host_capacity_cli_parser_and_json(monkeypatch, capsys, facts):
    args = cli_main.build_parser().parse_args(["host", "capacity", "--timeout", "3", "--json"])
    assert args.host_command == "capacity"
    assert args.as_json
    assert args.timeout_seconds == 3.0
    monkeypatch.setattr(capacity.platform, "system", lambda: "Linux")
    monkeypatch.setattr(
        capacity, "open",
        lambda *_a, **_kw: io.BytesIO(b"MemAvailable: 1024 kB\n"),
        raising=False,
    )
    assert cli_main.main(["host", "capacity", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["memory_available_bytes"] == 1_048_576
    assert payload["memory_availability_kind"] == "linux_memavailable"
    assert payload["root_free_bytes"] == 3_000


def test_host_capacity_cli_failure_is_json_error(monkeypatch, capsys):
    class Broken:
        def inspect(self, *, limits):
            raise HostProfileError("unavailable")

    monkeypatch.setattr(host_cmd, "HostCapacityProfiler", Broken)
    assert host_cmd.run_capacity(timeout_seconds=2.0, as_json=True) == 1
    assert json.loads(capsys.readouterr().err) == {"error": "unavailable"}
