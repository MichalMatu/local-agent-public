from __future__ import annotations

import importlib
import json

import pytest

from local_agent.host_ops.capabilities.local.host import HostProfile, HostProfileError

cli_main = importlib.import_module("local_agent.host_ops.cli.main")
host_cmd = importlib.import_module("local_agent.host_ops.cli.commands.host")


def test_host_profile_parser_exposes_bounded_timeout() -> None:
    args = cli_main.build_parser().parse_args(["host", "profile", "--timeout", "3", "--json"])

    assert args.command == "host"
    assert args.host_command == "profile"
    assert args.timeout_seconds == 3.0
    assert args.as_json is True


def test_run_profile_renders_json(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    class FakeProfiler:
        def inspect(self, *, limits):
            assert limits.timeout_seconds == 4.0
            return HostProfile(
                hostname="builder-01",
                system="Darwin",
                release="25.0.0",
                architecture="arm64",
                logical_cpu_count=10,
                memory_total_bytes=17_179_869_184,
                gpu_devices=("Apple M1",),
                root_total_bytes=1_000_000,
                root_free_bytes=600_000,
            )

    monkeypatch.setattr(host_cmd, "HostProfiler", FakeProfiler)

    assert host_cmd.run_profile(timeout_seconds=4.0, as_json=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["hostname"] == "builder-01"
    assert payload["memory_total_bytes"] == 17_179_869_184
    assert payload["gpu_devices"] == ["Apple M1"]


def test_run_profile_reports_failure(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    class FakeProfiler:
        def inspect(self, *, limits):
            raise HostProfileError("profile unavailable")

    monkeypatch.setattr(host_cmd, "HostProfiler", FakeProfiler)

    assert host_cmd.run_profile(timeout_seconds=4.0, as_json=True) == 1
    assert "unavailable" in json.loads(capsys.readouterr().err)["error"]
