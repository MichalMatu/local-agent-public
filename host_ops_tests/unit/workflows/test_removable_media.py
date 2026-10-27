from __future__ import annotations

import importlib
from pathlib import Path

import pytest

from local_agent.host_ops.capabilities.local.files import ArtifactDeploymentError, ArtifactDeploymentResult
from local_agent.host_ops.capabilities.local.macos import (
    MacOSInspectionError,
    MacOSStorageActionResult,
    MacOSStorageControlError,
    MacOSStorageDevice,
)
from local_agent.host_ops.core.execution import ExecutionLimits
from local_agent.host_ops.workflows.removable_media import (
    MacOSRemovableMediaDeployer,
    RemovableMediaDeploymentError,
)

workflow_module = importlib.import_module(
    "local_agent.host_ops.workflows.removable_media.deploy"
)


def _inventory(
    *,
    mount_point: str | None,
    volume_read_only: bool = False,
    media_read_only: bool = False,
) -> tuple[MacOSStorageDevice, MacOSStorageDevice]:
    whole = MacOSStorageDevice(
        identifier="disk4",
        whole=True,
        internal=False,
        read_only_media=media_read_only,
    )
    volume = MacOSStorageDevice(
        identifier="disk4s1",
        whole=False,
        part_of_whole="disk4",
        mount_point=mount_point,
        internal=False,
        read_only_volume=volume_read_only,
    )
    return whole, volume


class FakeInspector:
    def __init__(self, snapshots) -> None:
        self.snapshots = list(snapshots)
        self.calls = 0
        self.limits: list[ExecutionLimits | None] = []

    def external_storage(self, *, limits=None):
        self.calls += 1
        self.limits.append(limits)
        value = self.snapshots.pop(0)
        if isinstance(value, Exception):
            raise value
        return value


class FakeController:
    def __init__(self, *, eject_error: Exception | None = None) -> None:
        self.calls: list[tuple[str, str]] = []
        self.limits: list[ExecutionLimits | None] = []
        self.eject_error = eject_error

    def mount(self, identifier: str, *, limits=None):
        self.calls.append(("mount", identifier))
        self.limits.append(limits)
        return MacOSStorageActionResult(
            action="mount",
            identifier=identifier,
            message="mounted",
        )

    def eject(self, identifier: str, *, limits=None):
        self.calls.append(("eject", identifier))
        self.limits.append(limits)
        if self.eject_error is not None:
            raise self.eject_error
        return MacOSStorageActionResult(
            action="eject",
            identifier=identifier,
            message="ejected",
        )


class FakeDeployer:
    def __init__(self, *, error: Exception | None = None) -> None:
        self.calls: list[tuple[Path, Path, str | None, bool]] = []
        self.limits: list[ExecutionLimits | None] = []
        self.error = error

    def deploy(
        self,
        source: Path,
        destination_directory: Path,
        *,
        destination_name: str | None = None,
        replace: bool = False,
        limits: ExecutionLimits | None = None,
    ) -> ArtifactDeploymentResult:
        self.calls.append((source, destination_directory, destination_name, replace))
        self.limits.append(limits)
        if self.error is not None:
            raise self.error
        name = destination_name or source.name
        return ArtifactDeploymentResult(
            source=source,
            destination=destination_directory / name,
            size_bytes=7,
            sha256="a" * 64,
            replaced_existing=replace,
            directory_synced=True,
        )


def test_already_mounted_volume_deploys_without_storage_action() -> None:
    inspector = FakeInspector([_inventory(mount_point="/Volumes/FIRMWARE")])
    controller = FakeController()
    deployer = FakeDeployer()

    result = MacOSRemovableMediaDeployer(inspector, controller, deployer).deploy(
        Path("input.bin"),
        "disk4s1",
        destination_name="firmware.bin",
    )

    assert result.volume_identifier == "disk4s1"
    assert result.whole_disk_identifier == "disk4"
    assert result.mount_point == Path("/Volumes/FIRMWARE")
    assert result.mounted_by_workflow is False
    assert result.ejected is False
    assert controller.calls == []
    assert deployer.calls == [(Path("input.bin"), Path("/Volumes/FIRMWARE"), "firmware.bin", False)]


def test_unmounted_volume_is_mounted_then_deployed_and_ejected() -> None:
    inspector = FakeInspector(
        [
            _inventory(mount_point=None),
            _inventory(mount_point="/Volumes/FIRMWARE"),
        ]
    )
    controller = FakeController()
    deployer = FakeDeployer()

    result = MacOSRemovableMediaDeployer(inspector, controller, deployer).deploy(
        Path("input.bin"),
        "disk4s1",
        replace=True,
        eject=True,
    )

    assert result.mounted_by_workflow is True
    assert result.ejected is True
    assert result.eject_message == "ejected"
    assert controller.calls == [("mount", "disk4s1"), ("eject", "disk4")]
    assert deployer.calls[0][-1] is True


def test_whole_disk_identifier_is_rejected_for_artifact_deployment() -> None:
    inspector = FakeInspector([_inventory(mount_point="/Volumes/FIRMWARE")])

    with pytest.raises(RemovableMediaDeploymentError, match="requires a volume") as captured:
        MacOSRemovableMediaDeployer(inspector, FakeController(), FakeDeployer()).deploy(
            Path("input.bin"),
            "disk4",
        )

    assert captured.value.stage == "inspect"
    assert captured.value.volume_identifier == "disk4"


@pytest.mark.parametrize(
    ("volume_read_only", "media_read_only", "message"),
    [
        (True, False, "volume is read-only"),
        (False, True, "media is read-only"),
    ],
)
def test_read_only_target_is_rejected(
    volume_read_only: bool,
    media_read_only: bool,
    message: str,
) -> None:
    inspector = FakeInspector(
        [
            _inventory(
                mount_point="/Volumes/FIRMWARE",
                volume_read_only=volume_read_only,
                media_read_only=media_read_only,
            )
        ]
    )

    with pytest.raises(RemovableMediaDeploymentError, match=message):
        MacOSRemovableMediaDeployer(inspector, FakeController(), FakeDeployer()).deploy(
            Path("input.bin"),
            "disk4s1",
        )


@pytest.mark.parametrize("mount_point", ["/tmp/FIRMWARE", "//Volumes/FIRMWARE"])
def test_unexpected_mount_point_fails_closed_with_exact_identity(mount_point: str) -> None:
    inspector = FakeInspector([_inventory(mount_point=mount_point)])

    with pytest.raises(RemovableMediaDeploymentError, match="unexpected") as captured:
        MacOSRemovableMediaDeployer(inspector, FakeController(), FakeDeployer()).deploy(
            Path("input.bin"),
            "disk4s1",
        )

    assert captured.value.stage == "inspect"
    assert captured.value.volume_identifier == "disk4s1"


def test_failed_deploy_leaves_mounted_volume_for_operator_inspection() -> None:
    inspector = FakeInspector(
        [
            _inventory(mount_point=None),
            _inventory(mount_point="/Volumes/FIRMWARE"),
        ]
    )
    controller = FakeController()
    deployer = FakeDeployer(error=ArtifactDeploymentError("write failed"))

    with pytest.raises(RemovableMediaDeploymentError, match="write failed") as captured:
        MacOSRemovableMediaDeployer(inspector, controller, deployer).deploy(
            Path("input.bin"),
            "disk4s1",
            eject=True,
        )

    assert captured.value.stage == "deploy"
    assert captured.value.mounted_by_workflow is True
    assert captured.value.deployment is None
    assert controller.calls == [("mount", "disk4s1")]


def test_eject_failure_preserves_successful_deployment_evidence() -> None:
    inspector = FakeInspector([_inventory(mount_point="/Volumes/FIRMWARE")])
    controller = FakeController(
        eject_error=MacOSStorageControlError("device busy", action_attempted=True)
    )

    with pytest.raises(RemovableMediaDeploymentError, match="device busy") as captured:
        MacOSRemovableMediaDeployer(inspector, controller, FakeDeployer()).deploy(
            Path("input.bin"),
            "disk4s1",
            eject=True,
        )

    error = captured.value
    assert error.stage == "eject"
    assert error.deployment is not None
    assert error.as_dict()["artifact_deployed"] is True
    assert error.as_dict()["artifact_committed"] is True
    assert error.as_dict()["storage_action_attempted"] is True
    assert controller.calls == [("eject", "disk4")]


def test_post_mount_inspection_failure_reports_mount_stage() -> None:
    inspector = FakeInspector(
        [
            _inventory(mount_point=None),
            MacOSInspectionError("diskutil failed"),
        ]
    )

    with pytest.raises(RemovableMediaDeploymentError, match="diskutil failed") as captured:
        MacOSRemovableMediaDeployer(inspector, FakeController(), FakeDeployer()).deploy(
            Path("input.bin"),
            "disk4s1",
        )

    assert captured.value.stage == "mount"
    assert captured.value.mounted_by_workflow is True


def test_workflow_passes_one_decreasing_deadline_to_every_stage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inspector = FakeInspector(
        [
            _inventory(mount_point=None),
            _inventory(mount_point="/Volumes/FIRMWARE"),
        ]
    )
    controller = FakeController()
    deployer = FakeDeployer()
    ticks = iter((100.0, 101.0, 102.0, 103.0, 104.0, 105.0))
    monkeypatch.setattr(workflow_module.time, "monotonic", lambda: next(ticks))
    limits = ExecutionLimits(
        timeout_seconds=10.0,
        terminate_grace_seconds=2.0,
        pipe_drain_seconds=0.5,
        max_stdout_bytes=1234,
        max_stderr_bytes=5678,
    )

    MacOSRemovableMediaDeployer(inspector, controller, deployer).deploy(
        Path("input.bin"),
        "disk4s1",
        eject=True,
        limits=limits,
    )

    seen = [
        inspector.limits[0],
        controller.limits[0],
        inspector.limits[1],
        deployer.limits[0],
        controller.limits[1],
    ]
    assert [value.timeout_seconds for value in seen if value is not None] == [
        9.0,
        8.0,
        7.0,
        6.0,
        5.0,
    ]
    for value in seen:
        assert value is not None
        assert value.terminate_grace_seconds == 2.0
        assert value.pipe_drain_seconds == 0.5
        assert value.max_stdout_bytes == 1234
        assert value.max_stderr_bytes == 5678


def test_workflow_deadline_exhaustion_after_mount_preserves_mount_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inspector = FakeInspector([_inventory(mount_point=None)])
    controller = FakeController()
    deployer = FakeDeployer()
    ticks = iter((100.0, 101.0, 102.0, 110.0))
    monkeypatch.setattr(workflow_module.time, "monotonic", lambda: next(ticks))

    with pytest.raises(RemovableMediaDeploymentError, match="whole-operation timeout") as captured:
        MacOSRemovableMediaDeployer(inspector, controller, deployer).deploy(
            Path("input.bin"),
            "disk4s1",
            limits=ExecutionLimits(timeout_seconds=10.0),
        )

    assert captured.value.stage == "mount"
    assert captured.value.mounted_by_workflow is True
    assert inspector.calls == 1
    assert controller.calls == [("mount", "disk4s1")]
    assert deployer.calls == []


def test_workflow_deadline_exhaustion_before_eject_preserves_deployment_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inspector = FakeInspector([_inventory(mount_point="/Volumes/FIRMWARE")])
    controller = FakeController()
    deployer = FakeDeployer()
    ticks = iter((100.0, 101.0, 102.0, 110.0))
    monkeypatch.setattr(workflow_module.time, "monotonic", lambda: next(ticks))

    with pytest.raises(RemovableMediaDeploymentError, match="whole-operation timeout") as captured:
        MacOSRemovableMediaDeployer(inspector, controller, deployer).deploy(
            Path("input.bin"),
            "disk4s1",
            eject=True,
            limits=ExecutionLimits(timeout_seconds=10.0),
        )

    error = captured.value
    payload = error.as_dict()
    assert error.stage == "eject"
    assert error.deployment is not None
    assert payload["artifact_deployed"] is True
    assert payload["artifact_committed"] is True
    assert payload["storage_action_attempted"] is False
    assert controller.calls == []


def test_deploy_error_reports_committed_but_unverified_artifact() -> None:
    inspector = FakeInspector([_inventory(mount_point="/Volumes/FIRMWARE")])
    deployer = FakeDeployer(
        error=ArtifactDeploymentError(
            "artifact deployment exceeded its whole-operation timeout after commit",
            committed=True,
        )
    )

    with pytest.raises(RemovableMediaDeploymentError) as captured:
        MacOSRemovableMediaDeployer(inspector, FakeController(), deployer).deploy(
            Path("input.bin"),
            "disk4s1",
        )

    payload = captured.value.as_dict()
    assert payload["artifact_deployed"] is False
    assert payload["artifact_committed"] is True
