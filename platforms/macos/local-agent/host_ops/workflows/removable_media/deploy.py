"""Compose macOS external-storage control with verified local artifact deployment."""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from local_agent.host_ops.capabilities.local.files import (
    ArtifactDeploymentError,
    ArtifactDeploymentResult,
    LocalArtifactDeployer,
)
from local_agent.host_ops.capabilities.local.macos import (
    MacOSInspectionError,
    MacOSInspector,
    MacOSStorageControlError,
    MacOSStorageController,
    MacOSStorageDevice,
)
from local_agent.host_ops.core.execution import ExecutionLimits

_DEFAULT_LIMITS = ExecutionLimits()


class RemovableMediaDeploymentError(RuntimeError):
    """Report a failed workflow stage without hiding completed side effects."""

    def __init__(
        self,
        message: str,
        *,
        stage: str,
        volume_identifier: str,
        mounted_by_workflow: bool = False,
        deployment: ArtifactDeploymentResult | None = None,
        artifact_committed: bool = False,
        storage_action_attempted: bool = False,
    ) -> None:
        super().__init__(message)
        self.stage = stage
        self.volume_identifier = volume_identifier
        self.mounted_by_workflow = mounted_by_workflow
        self.deployment = deployment
        self.artifact_committed = artifact_committed
        self.storage_action_attempted = storage_action_attempted

    def as_dict(self) -> dict[str, Any]:
        return {
            "error": str(self),
            "stage": self.stage,
            "volume_identifier": self.volume_identifier,
            "mounted_by_workflow": self.mounted_by_workflow,
            "artifact_deployed": self.deployment is not None,
            "artifact_committed": self.artifact_committed,
            "storage_action_attempted": self.storage_action_attempted,
            "deployment": None if self.deployment is None else self.deployment.as_dict(),
        }


class _WorkflowBudget:
    """One monotonic deadline shared by the complete removable-media workflow."""

    def __init__(self, limits: ExecutionLimits) -> None:
        self._limits = limits
        self._deadline = time.monotonic() + limits.timeout_seconds

    def remaining(
        self,
        *,
        stage: str,
        volume_identifier: str,
        mounted_by_workflow: bool = False,
        deployment: ArtifactDeploymentResult | None = None,
    ) -> ExecutionLimits:
        remaining = self._deadline - time.monotonic()
        if remaining <= 0:
            raise RemovableMediaDeploymentError(
                "removable media deployment exceeded its whole-operation timeout",
                stage=stage,
                volume_identifier=volume_identifier,
                mounted_by_workflow=mounted_by_workflow,
                deployment=deployment,
                artifact_committed=deployment is not None,
            )
        return ExecutionLimits(
            timeout_seconds=remaining,
            terminate_grace_seconds=self._limits.terminate_grace_seconds,
            pipe_drain_seconds=self._limits.pipe_drain_seconds,
            max_stdout_bytes=self._limits.max_stdout_bytes,
            max_stderr_bytes=self._limits.max_stderr_bytes,
        )


@dataclass(frozen=True, slots=True)
class RemovableMediaDeploymentResult:
    volume_identifier: str
    whole_disk_identifier: str
    mount_point: Path
    mounted_by_workflow: bool
    ejected: bool
    deployment: ArtifactDeploymentResult
    eject_message: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "volume_identifier": self.volume_identifier,
            "whole_disk_identifier": self.whole_disk_identifier,
            "mount_point": str(self.mount_point),
            "mounted_by_workflow": self.mounted_by_workflow,
            "ejected": self.ejected,
            "eject_message": self.eject_message,
            "deployment": self.deployment.as_dict(),
        }


class MacOSRemovableMediaDeployer:
    """Deploy one artifact to one explicit external macOS volume."""

    def __init__(
        self,
        inspector: MacOSInspector | None = None,
        controller: MacOSStorageController | None = None,
        deployer: LocalArtifactDeployer | None = None,
    ) -> None:
        self._inspector = inspector or MacOSInspector()
        self._controller = controller or MacOSStorageController()
        self._deployer = deployer or LocalArtifactDeployer()

    def deploy(
        self,
        source: Path,
        volume_identifier: str,
        *,
        destination_name: str | None = None,
        replace: bool = False,
        eject: bool = False,
        limits: ExecutionLimits | None = None,
    ) -> RemovableMediaDeploymentResult:
        budget = _WorkflowBudget(limits or _DEFAULT_LIMITS)
        mounted_by_workflow = False
        try:
            volume, whole = self._inspect_target(
                volume_identifier,
                limits=budget.remaining(
                    stage="inspect",
                    volume_identifier=volume_identifier,
                ),
            )
        except (MacOSInspectionError, RemovableMediaDeploymentError) as exc:
            if isinstance(exc, RemovableMediaDeploymentError):
                raise
            raise RemovableMediaDeploymentError(
                str(exc),
                stage="inspect",
                volume_identifier=volume_identifier,
            ) from exc

        mount_point = _validated_mount_point(
            volume.mount_point,
            volume_identifier=volume_identifier,
            stage="inspect",
            mounted_by_workflow=False,
        )
        if mount_point is None:
            try:
                self._controller.mount(
                    volume_identifier,
                    limits=budget.remaining(
                        stage="mount",
                        volume_identifier=volume_identifier,
                    ),
                )
            except MacOSStorageControlError as exc:
                raise RemovableMediaDeploymentError(
                    str(exc),
                    stage="mount",
                    volume_identifier=volume_identifier,
                    storage_action_attempted=exc.action_attempted,
                ) from exc
            mounted_by_workflow = True
            try:
                volume, whole = self._inspect_target(
                    volume_identifier,
                    limits=budget.remaining(
                        stage="mount",
                        volume_identifier=volume_identifier,
                        mounted_by_workflow=True,
                    ),
                )
            except (MacOSInspectionError, RemovableMediaDeploymentError) as exc:
                raise RemovableMediaDeploymentError(
                    str(exc),
                    stage="mount",
                    volume_identifier=volume_identifier,
                    mounted_by_workflow=True,
                ) from exc
            mount_point = _validated_mount_point(
                volume.mount_point,
                volume_identifier=volume_identifier,
                stage="mount",
                mounted_by_workflow=True,
            )
            if mount_point is None:
                raise RemovableMediaDeploymentError(
                    "volume has no mount point after successful mount",
                    stage="mount",
                    volume_identifier=volume_identifier,
                    mounted_by_workflow=True,
                )

        try:
            deployment = self._deployer.deploy(
                source,
                mount_point,
                destination_name=destination_name,
                replace=replace,
                limits=budget.remaining(
                    stage="deploy",
                    volume_identifier=volume_identifier,
                    mounted_by_workflow=mounted_by_workflow,
                ),
            )
        except ArtifactDeploymentError as exc:
            raise RemovableMediaDeploymentError(
                str(exc),
                stage="deploy",
                volume_identifier=volume_identifier,
                mounted_by_workflow=mounted_by_workflow,
                artifact_committed=exc.committed,
            ) from exc

        eject_message: str | None = None
        if eject:
            try:
                eject_result = self._controller.eject(
                    whole.identifier,
                    limits=budget.remaining(
                        stage="eject",
                        volume_identifier=volume_identifier,
                        mounted_by_workflow=mounted_by_workflow,
                        deployment=deployment,
                    ),
                )
            except MacOSStorageControlError as exc:
                raise RemovableMediaDeploymentError(
                    str(exc),
                    stage="eject",
                    volume_identifier=volume_identifier,
                    mounted_by_workflow=mounted_by_workflow,
                    deployment=deployment,
                    artifact_committed=True,
                    storage_action_attempted=exc.action_attempted,
                ) from exc
            eject_message = eject_result.message

        return RemovableMediaDeploymentResult(
            volume_identifier=volume.identifier,
            whole_disk_identifier=whole.identifier,
            mount_point=mount_point,
            mounted_by_workflow=mounted_by_workflow,
            ejected=eject,
            deployment=deployment,
            eject_message=eject_message,
        )

    def _inspect_target(
        self,
        volume_identifier: str,
        *,
        limits: ExecutionLimits | None,
    ) -> tuple[MacOSStorageDevice, MacOSStorageDevice]:
        devices = self._inspector.external_storage(limits=limits)
        volume = next(
            (device for device in devices if device.identifier == volume_identifier),
            None,
        )
        if volume is None:
            raise RemovableMediaDeploymentError(
                "explicit volume identifier is not present in external storage inventory",
                stage="inspect",
                volume_identifier=volume_identifier,
            )
        if volume.internal is not False:
            raise RemovableMediaDeploymentError(
                "target volume is not confirmed external",
                stage="inspect",
                volume_identifier=volume_identifier,
            )
        if volume.whole:
            raise RemovableMediaDeploymentError(
                "artifact deployment requires a volume identifier such as disk4s1",
                stage="inspect",
                volume_identifier=volume_identifier,
            )
        if volume.read_only_volume is True:
            raise RemovableMediaDeploymentError(
                "target volume is read-only",
                stage="inspect",
                volume_identifier=volume_identifier,
            )
        if volume.part_of_whole is None:
            raise RemovableMediaDeploymentError(
                "target volume does not identify its containing whole disk",
                stage="inspect",
                volume_identifier=volume_identifier,
            )

        whole = next(
            (device for device in devices if device.identifier == volume.part_of_whole),
            None,
        )
        if whole is None or not whole.whole or whole.internal is not False:
            raise RemovableMediaDeploymentError(
                "containing whole disk is not confirmed external",
                stage="inspect",
                volume_identifier=volume_identifier,
            )
        if whole.read_only_media is True:
            raise RemovableMediaDeploymentError(
                "target media is read-only",
                stage="inspect",
                volume_identifier=volume_identifier,
            )
        return volume, whole


def _validated_mount_point(
    value: str | None,
    *,
    volume_identifier: str,
    stage: str,
    mounted_by_workflow: bool,
) -> Path | None:
    if value is None:
        return None
    path = Path(value)
    if (
        not path.is_absolute()
        or path.anchor != "/"
        or ".." in path.parts
        or len(path.parts) < 3
        or path.parts[1] != "Volumes"
    ):
        raise RemovableMediaDeploymentError(
            f"unexpected external volume mount point: {value}",
            stage=stage,
            volume_identifier=volume_identifier,
            mounted_by_workflow=mounted_by_workflow,
        )
    return path
