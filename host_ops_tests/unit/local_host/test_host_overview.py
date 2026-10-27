"""Offline A1 host overview tests: one budget, privacy and no authority."""
from __future__ import annotations

import importlib
import json

import pytest

from local_agent.host_ops.capabilities.local.host import (
    HostCapacitySnapshot, HostOverviewCollector, HostProfile, HostProfileError,
)
from local_agent.host_ops.capabilities.local.tools.models import ToolInspectionResult
from local_agent.host_ops.core.execution import ExecutionLimits

overview_mod = importlib.import_module("local_agent.host_ops.capabilities.local.host.overview")
host_cmd = importlib.import_module("local_agent.host_ops.cli.commands.host")
cli_main = importlib.import_module("local_agent.host_ops.cli.main")


class FakeProfile:
    def __init__(self) -> None:
        self.calls = []

    def inspect(self, *, limits):
        self.calls.append(limits)
        return HostProfile(
            hostname="builder", system="Darwin", release="25.0", architecture="arm64",
            logical_cpu_count=8, memory_total_bytes=8_000_000_000,
            root_total_bytes=10000, root_free_bytes=5000,
        )


class FakeCapacity:
    def __init__(self) -> None:
        self.calls = []

    def inspect(self, *, limits):
        self.calls.append(limits)
        return HostCapacitySnapshot(
            observed_at_utc="2026-10-21T18:00:00+00:00", system="Darwin",
            logical_cpu_count=8, memory_available_bytes=3_000_000,
            memory_availability_kind="macos_vm_stat_estimate",
            load_average_1m=1.0, load_average_5m=2.0, load_average_15m=3.0,
            root_total_bytes=10000, root_free_bytes=5000,
        )


class FakeTools:
    def __init__(self) -> None:
        self.calls = []

    def inspect(self, names, *, limits):
        self.calls.append((names, limits))
        return (ToolInspectionResult(
            name=names[0], present=True,
            resolved_path="/Users/private/custom/bin/" + names[0],
            version=names[0] + " version 1", version_exit_code=0,
        ),)


def test_overview_returns_expected_stable_fields_without_private_paths():
    host, capacity, tools = FakeProfile(), FakeCapacity(), FakeTools()
    result = HostOverviewCollector(host=host, capacity=capacity, tools=tools).inspect(
        names=("git", "node", "git")
    )
    assert result["contract_version"] == 1
    assert result["interpretation"] == "diagnostic_only_not_reserved_capacity"
    assert result["profile"]["hostname"] == "builder"
    assert result["capacity"]["memory_available_bytes"] == 3_000_000
    assert [x["name"] for x in result["tools"]] == ["git", "node"]
    assert len(host.calls) == len(capacity.calls) == 1
    assert len(tools.calls) == 2
    assert "/Users/private" not in json.dumps(result)
    assert "resolved_path" not in json.dumps(result)


def test_bad_tool_name_fails_before_any_probe():
    host = FakeProfile()
    with pytest.raises(ValueError, match="invalid tool name"):
        HostOverviewCollector(host=host).inspect(names=("git", "../../unsafe"))
    assert host.calls == []


def test_more_than_twelve_tools_rejected():
    with pytest.raises(ValueError, match="at most 12"):
        HostOverviewCollector().inspect(names=tuple(f"tool{i}" for i in range(13)))


def test_one_budget_is_shared_across_all_collectors(monkeypatch):
    host, capacity, tools = FakeProfile(), FakeCapacity(), FakeTools()
    ticks = iter((100.0, 101.0, 102.0, 103.0, 105.0))
    monkeypatch.setattr(overview_mod.time, "monotonic", lambda: next(ticks))
    with pytest.raises(HostProfileError, match="whole-operation timeout"):
        HostOverviewCollector(host=host, capacity=capacity, tools=tools).inspect(
            names=("git",), limits=ExecutionLimits(timeout_seconds=4.0)
        )
    assert host.calls[0].timeout_seconds == 3
    assert capacity.calls[0].timeout_seconds == 2
    assert tools.calls[0][1].timeout_seconds == 1


def test_failure_detail_from_tool_is_redacted():
    class BrokenTools:
        def inspect(self, names, *, limits):
            return (ToolInspectionResult(
                name=names[0], present=True, version=None,
                error="unsafe /Users/private/path",
            ),)

    data = HostOverviewCollector(
        host=FakeProfile(), capacity=FakeCapacity(), tools=BrokenTools()
    ).inspect(names=("git",))
    assert data["tools"][0]["error"] == "version_probe_failed"
    assert "private" not in json.dumps(data)


def test_cli_parser_and_json_output(monkeypatch, capsys):
    args = cli_main.build_parser().parse_args([
        "host", "overview", "--tools", "git", "python3", "--timeout", "9", "--json",
    ])
    assert args.host_command == "overview"
    assert args.tools == ["git", "python3"]
    assert args.timeout_seconds == 9
    assert args.as_json

    class FakeCollector:
        def inspect(self, *, names, limits):
            assert names == ("git", "python3")
            assert limits.timeout_seconds == 9
            return {"contract_version": 1, "profile": {}, "capacity": {}, "tools": []}

    monkeypatch.setattr(host_cmd, "HostOverviewCollector", FakeCollector)
    assert cli_main.main([
        "host", "overview", "--tools", "git", "python3", "--timeout", "9", "--json",
    ]) == 0
    assert json.loads(capsys.readouterr().out)["contract_version"] == 1


def test_cli_error_is_explicit(monkeypatch, capsys):
    class BrokenCollector:
        def inspect(self, *, names, limits):
            raise HostProfileError("budget exhausted")

    monkeypatch.setattr(host_cmd, "HostOverviewCollector", BrokenCollector)
    assert host_cmd.run_overview(
        timeout_seconds=1, names=("git",), as_json=True,
    ) == 1
    assert json.loads(capsys.readouterr().err) == {"error": "budget exhausted"}


def test_human_overview_is_readable_and_does_not_claim_free_workers(monkeypatch, capsys):
    class FakeCollector:
        def inspect(self, *, names, limits):
            return {
                "contract_version": 1,
                "profile": FakeProfile().inspect(limits=limits).as_dict(),
                "capacity": FakeCapacity().inspect(limits=limits).as_dict(),
                "tools": [
                    {"name": "git", "present": True, "version": "git version 2.50",
                     "version_exit_code": 0, "error": None},
                    {"name": "ninja", "present": False, "version": None,
                     "version_exit_code": None, "error": None},
                ],
                "observed_at_utc": "2026-10-21T18:00:00+00:00",
            }

    monkeypatch.setattr(host_cmd, "HostOverviewCollector", FakeCollector)
    assert host_cmd.run_overview(timeout_seconds=15.0, names=("git", "ninja"),
                                 as_json=False) == 0
    result = capsys.readouterr().out
    assert "Host: builder | Darwin" in result
    assert "CPU: 8 logical | load 1/5/15m: 1.00 / 2.00 / 3.00" in result
    assert "not CPU percent" in result
    assert "macOS reclaimable-page estimate" in result
    assert "Memory: 2.9 MiB available" in result
    assert "git: git version 2.50" in result
    assert "ninja: not found" in result
    assert "Diagnostic only: not reserved capacity or idle worker slots." in result
    assert "/Users/private" not in result
