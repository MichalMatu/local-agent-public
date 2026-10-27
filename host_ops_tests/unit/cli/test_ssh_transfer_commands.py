from __future__ import annotations

import importlib
import json
from pathlib import Path

import pytest

from local_agent.host_ops.capabilities.remote.ssh import SshTransferError, SshTransferResult
from local_agent.host_ops.cli.host_target import HostTargetResolutionError
from local_agent.host_ops.core.config import HostTarget

ssh_cmd = importlib.import_module("local_agent.host_ops.cli.commands.ssh")


def _target() -> HostTarget:
    return HostTarget(
        alias="termux-phone",
        host="192.0.2.100",
        port=8022,
        user="u0_a123",
        identity_file="/tmp/test-key",
    )


def test_run_push_emits_json_and_passes_transfer_limits(
    monkeypatch: pytest.MonkeyPatch,
    capsys,
) -> None:
    monkeypatch.setattr(ssh_cmd, "load_host_target", lambda alias: _target())

    class FakeTransfer:
        def push(self, target, source: Path, destination: str, **kwargs):
            assert target.alias == "termux-phone"
            assert source == Path("firmware.bin")
            assert destination == "/data/local/firmware.bin"
            assert kwargs["replace"] is True
            assert kwargs["max_bytes"] == 1234
            assert kwargs["limits"].timeout_seconds == 12.0
            return SshTransferResult(
                direction="push",
                target=target.alias,
                source=str(source),
                destination=destination,
                size_bytes=7,
                sha256="a" * 64,
                replaced_existing=True,
            )

    monkeypatch.setattr(ssh_cmd, "SshFileTransfer", FakeTransfer)

    code = ssh_cmd.run_push(
        "termux-phone",
        "firmware.bin",
        "/data/local/firmware.bin",
        replace=True,
        max_bytes=1234,
        timeout_seconds=12.0,
        as_json=True,
    )

    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["direction"] == "push"
    assert payload["sha256"] == "a" * 64
    assert payload["replaced_existing"] is True


def test_run_pull_renders_directory_sync_warning(
    monkeypatch: pytest.MonkeyPatch,
    capsys,
) -> None:
    monkeypatch.setattr(ssh_cmd, "load_host_target", lambda alias: _target())

    class FakeTransfer:
        def pull(self, target, source: str, destination: Path, **kwargs):
            return SshTransferResult(
                direction="pull",
                target=target.alias,
                source=source,
                destination=str(destination),
                size_bytes=4,
                sha256="b" * 64,
                replaced_existing=False,
                local_directory_synced=False,
            )

    monkeypatch.setattr(ssh_cmd, "SshFileTransfer", FakeTransfer)

    code = ssh_cmd.run_pull(
        "termux-phone",
        "/remote/result.bin",
        "result.bin",
        replace=False,
        max_bytes=4096,
        timeout_seconds=30.0,
        as_json=False,
    )

    captured = capsys.readouterr()
    assert code == 0
    assert "pulled /remote/result.bin -> result.bin" in captured.out
    assert "does not support directory fsync" in captured.err


def test_run_push_reports_transfer_failure_as_runtime_error(
    monkeypatch: pytest.MonkeyPatch,
    capsys,
) -> None:
    monkeypatch.setattr(ssh_cmd, "load_host_target", lambda alias: _target())

    class FakeTransfer:
        def push(self, *args, **kwargs):
            raise SshTransferError(
                "digest mismatch",
                action_attempted=True,
                committed=True,
                cleanup_failed=True,
            )

    monkeypatch.setattr(ssh_cmd, "SshFileTransfer", FakeTransfer)

    code = ssh_cmd.run_push(
        "termux-phone",
        "firmware.bin",
        "/remote/firmware.bin",
        replace=False,
        max_bytes=4096,
        timeout_seconds=30.0,
        as_json=True,
    )

    assert code == 1
    payload = json.loads(capsys.readouterr().err)
    assert "digest mismatch" in payload["error"]
    assert payload["ok"] is False
    assert payload["action_attempted"] is True
    assert payload["committed"] is True
    assert payload["cleanup_failed"] is True


def test_run_pull_reports_unknown_target_as_input_error(
    monkeypatch: pytest.MonkeyPatch,
    capsys,
) -> None:
    def fail(_alias: str):
        raise HostTargetResolutionError("unknown host alias")

    monkeypatch.setattr(ssh_cmd, "load_host_target", fail)

    code = ssh_cmd.run_pull(
        "missing",
        "/remote/result.bin",
        "result.bin",
        replace=False,
        max_bytes=4096,
        timeout_seconds=30.0,
        as_json=True,
    )

    assert code == 2
    assert "unknown host alias" in json.loads(capsys.readouterr().out)["error"]


def test_run_push_rejects_invalid_timeout_before_transfer(
    monkeypatch: pytest.MonkeyPatch,
    capsys,
) -> None:
    monkeypatch.setattr(ssh_cmd, "load_host_target", lambda alias: _target())

    class FakeTransfer:
        def push(self, *args, **kwargs):
            raise AssertionError("transfer should not be called")

    monkeypatch.setattr(ssh_cmd, "SshFileTransfer", FakeTransfer)

    code = ssh_cmd.run_push(
        "termux-phone",
        "firmware.bin",
        "/remote/firmware.bin",
        replace=False,
        max_bytes=4096,
        timeout_seconds=0.0,
        as_json=True,
    )

    assert code == 2
    assert "timeout_seconds" in json.loads(capsys.readouterr().out)["error"]


def test_run_push_uncertain_commit_uses_nullable_json_effect(
    monkeypatch: pytest.MonkeyPatch, capsys,
) -> None:
    monkeypatch.setattr(ssh_cmd, "load_host_target", lambda alias: _target())

    class FakeTransfer:
        def push(self, *args, **kwargs):
            raise SshTransferError(
                "SSH response lost during remote commit",
                action_attempted=True,
                committed=False,
                commit_uncertain=True,
                code="ssh_commit_uncertain",
                phase="remote_commit",
            )

    monkeypatch.setattr(ssh_cmd, "SshFileTransfer", FakeTransfer)
    status = ssh_cmd.run_push(
        "termux-phone", "data.bin", "/remote/data.bin",
        replace=False, max_bytes=4096, timeout_seconds=10.0, as_json=True,
    )
    assert status == 1
    payload = json.loads(capsys.readouterr().err)
    assert payload["code"] == "ssh_commit_uncertain"
    assert payload["phase"] == "remote_commit"
    assert payload["action_attempted"] is True
    assert payload["committed"] is None
    assert payload["commit_uncertain"] is True
