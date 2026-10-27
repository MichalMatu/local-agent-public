from __future__ import annotations

import importlib
import json
from pathlib import Path

import pytest

from local_agent.host_ops.capabilities.local.files import ArtifactDeploymentResult
from local_agent.host_ops.workflows.removable_media import (
    RemovableMediaDeploymentError,
    RemovableMediaDeploymentResult,
)

cli_main = importlib.import_module("local_agent.host_ops.cli.main")
media_cmd = importlib.import_module("local_agent.host_ops.cli.commands.removable_media")


def _deployment() -> ArtifactDeploymentResult:
    return ArtifactDeploymentResult(
        source=Path("input.bin"),
        destination=Path("/Volumes/FIRMWARE/firmware.bin"),
        size_bytes=7,
        sha256="a" * 64,
        replaced_existing=False,
        directory_synced=True,
    )


def test_macos_deploy_media_parser_exposes_explicit_volume_contract() -> None:
    args = cli_main.build_parser().parse_args(
        [
            "macos",
            "deploy-media",
            "disk4s1",
            "input.bin",
            "--name",
            "firmware.bin",
            "--replace",
            "--eject",
            "--timeout",
            "9",
            "--json",
        ]
    )

    assert args.macos_command == "deploy-media"
    assert args.identifier == "disk4s1"
    assert args.source == "input.bin"
    assert args.destination_name == "firmware.bin"
    assert args.replace is True
    assert args.eject is True
    assert args.timeout_seconds == 9.0
    assert args.as_json is True


def test_run_deploy_renders_structured_success(
    monkeypatch: pytest.MonkeyPatch,
    capsys,
) -> None:
    class FakeWorkflow:
        def deploy(self, source, volume_identifier, **kwargs):
            assert source == Path("input.bin")
            assert volume_identifier == "disk4s1"
            assert kwargs["destination_name"] == "firmware.bin"
            assert kwargs["replace"] is True
            assert kwargs["eject"] is True
            assert kwargs["limits"].timeout_seconds == 8.0
            return RemovableMediaDeploymentResult(
                volume_identifier="disk4s1",
                whole_disk_identifier="disk4",
                mount_point=Path("/Volumes/FIRMWARE"),
                mounted_by_workflow=True,
                ejected=True,
                deployment=_deployment(),
                eject_message="ejected",
            )

    monkeypatch.setattr(media_cmd, "MacOSRemovableMediaDeployer", FakeWorkflow)

    assert (
        media_cmd.run_deploy(
            "disk4s1",
            "input.bin",
            destination_name="firmware.bin",
            replace=True,
            eject=True,
            timeout_seconds=8.0,
            as_json=True,
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["whole_disk_identifier"] == "disk4"
    assert payload["deployment"]["sha256"] == "a" * 64
    assert payload["ejected"] is True


def test_run_deploy_reports_partial_effect_error(
    monkeypatch: pytest.MonkeyPatch,
    capsys,
) -> None:
    class FakeWorkflow:
        def deploy(self, source, volume_identifier, **kwargs):
            raise RemovableMediaDeploymentError(
                "device busy",
                stage="eject",
                volume_identifier=volume_identifier,
                mounted_by_workflow=False,
                deployment=_deployment(),
            )

    monkeypatch.setattr(media_cmd, "MacOSRemovableMediaDeployer", FakeWorkflow)

    assert (
        media_cmd.run_deploy(
            "disk4s1",
            "input.bin",
            destination_name=None,
            replace=False,
            eject=True,
            timeout_seconds=8.0,
            as_json=True,
        )
        == 1
    )
    payload = json.loads(capsys.readouterr().err)
    assert payload["stage"] == "eject"
    assert payload["artifact_deployed"] is True
    assert payload["deployment"]["destination"].endswith("firmware.bin")
