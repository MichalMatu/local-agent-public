from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from local_agent.host_ops.capabilities.local.adb import AdbClient, AdbTransferError
from local_agent.host_ops.capabilities.local.adb.models import AdbTransferResult
from local_agent.host_ops.capabilities.local.files import (
    ArtifactDeploymentError,
    LocalArtifactDeployer,
    LocalArtifactInspector,
)
from local_agent.host_ops.capabilities.local.files.bounds import ArtifactOperationTimeout
from local_agent.host_ops.capabilities.remote.ssh import (
    SshClient,
    SshFileTransfer,
    SshTransferError,
    SshTransferResult,
)
from local_agent.host_ops.core.config import HostTarget
from local_agent.host_ops.core.execution import ExecutionLimits, ProcessResult, ProcessState
from local_agent.tool_runtime.contract import AuthorityCeiling, SemanticEffect
from local_agent.tool_runtime.host_ops_adapter import (
    ADB_IDENTITY_TOOL,
    ADB_LOGCAT_TOOL,
    ADB_PUSH_TOOL,
    ADB_PULL_TOOL,
    adb_push_invocation,
    adb_push_result,
    adb_push_error,
    adb_pull_invocation,
    adb_pull_result,
    adb_pull_error,
    ARTIFACT_INSPECT_TOOL,
    ARTIFACT_DEPLOY_TOOL,
    SSH_CHECK_TOOL,
    SSH_PULL_TOOL,
    SSH_PUSH_TOOL,
    adb_identity_invocation,
    adb_identity_result,
    adb_logcat_invocation,
    adb_logcat_result,
    artifact_inspect_invocation,
    artifact_inspect_result,
    artifact_deploy_invocation,
    artifact_deploy_result,
    artifact_deploy_error,
    ssh_check_invocation,
    ssh_check_result,
    ssh_pull_error,
    ssh_pull_invocation,
    ssh_pull_result,
    ssh_push_error,
    ssh_push_invocation,
)


_TEST_IDENTITY = "/tmp/host_ops_test_key"


def _completed(stdout: str = "") -> ProcessResult:
    return ProcessResult(
        state=ProcessState.COMPLETED,
        exit_code=0,
        stdout=stdout,
        stderr="",
        duration_seconds=0.001,
    )


class _Runner:
    def __init__(self, result: ProcessResult | None = None) -> None:
        self.result = result or _completed()
        self.commands: list[tuple[str, ...]] = []
        self.limits: list[ExecutionLimits | None] = []

    def run(self, command, *, limits=None, **_kwargs):
        self.commands.append(tuple(command))
        self.limits.append(limits)
        return self.result


def _target() -> HostTarget:
    return HostTarget(
        alias="phone",
        host="example.test",
        port=8022,
        user="u0_a123",
        identity_file=_TEST_IDENTITY,
    )


class _AdbRunner:
    def __init__(self, serial: str) -> None:
        self.serial = serial
        self.commands: list[tuple[str, ...]] = []
        self.properties = {
            "ro.product.manufacturer": "samsung",
            "ro.product.model": "SM-S906B",
            "ro.product.name": "r0sxxx",
            "ro.product.device": "r0s",
            "ro.build.version.release": "16",
            "ro.build.version.sdk": "36",
            "ro.build.fingerprint": "samsung/r0sxxx/r0s:16/test:user/release-keys",
        }

    def run(self, command, *, limits=None, **_kwargs):
        argv = tuple(command)
        self.commands.append(argv)
        if argv[-2:] == ("devices", "-l"):
            return _completed(
                "List of devices attached\n"
                f"{self.serial} device product:r0sxxx model:SM-S906B "
                "device:r0s transport_id:1\n"
            )
        if len(argv) >= 6 and argv[-2] == "getprop":
            return _completed(self.properties[argv[-1]] + "\n")
        raise AssertionError(f"unexpected ADB command: {argv!r}")

class _AdbLogcatRunner:
    def __init__(self, serial: str, text: str) -> None:
        self.serial = serial
        self.text = text
        self.commands: list[tuple[str, ...]] = []

    def run(self, command, *, limits=None, **_kwargs):
        argv = tuple(command)
        self.commands.append(argv)
        if argv[-1] == "get-state":
            return _completed("device\n")
        if "logcat" in argv:
            return _completed(self.text)
        raise AssertionError(f"unexpected ADB command: {argv!r}")


class ToolRuntimeHostOpsAdapterTests(unittest.TestCase):
    def test_adb_identity_projects_real_client_without_promoting_wireless_locator(
        self,
    ) -> None:
        serial = "192.0.2.100:38871"
        limits = ExecutionLimits(timeout_seconds=7.0)
        runner = _AdbRunner(serial)
        invocation = adb_identity_invocation(
            serial,
            limits=limits,
            scheduler_resources=(),
        )
        legacy = AdbClient(
            runner,
            resolver=lambda name: "/opt/android/platform-tools/adb"
            if name == "adb"
            else None,
        ).identity(serial, limits=limits)
        projected = adb_identity_result(invocation, legacy)

        self.assertEqual(projected.payload, legacy.as_dict())
        self.assertEqual(projected.tool, ADB_IDENTITY_TOOL)
        self.assertEqual(projected.tool.effect, SemanticEffect.ACTIVE_READ)
        self.assertEqual(
            projected.tool.authority,
            AuthorityCeiling.FIXED_REMOTE_DEVICE_EXEC,
        )
        self.assertEqual(invocation.scheduler_resources, ())
        self.assertEqual(projected.target.name, serial)
        self.assertEqual(projected.target.locator.kind, "adb_serial")
        self.assertEqual(projected.target.locator.attributes, {"serial": serial})
        identity = projected.target.identity_evidence[0]
        self.assertEqual(identity.kind, "adb_identity_profile")
        self.assertEqual(identity.attributes["model"], "SM-S906B")
        self.assertEqual(
            identity.attributes["build_fingerprint"],
            "samsung/r0sxxx/r0s:16/test:user/release-keys",
        )


    def test_adb_logcat_projects_bounded_debug_output_without_identity_invention(
        self,
    ) -> None:
        serial = "192.0.2.100:38871"
        limits = ExecutionLimits(
            timeout_seconds=7.0,
            max_stdout_bytes=32 * 1024,
            max_stderr_bytes=8 * 1024,
        )
        runner = _AdbLogcatRunner(serial, "line one\nline two\n")
        invocation = adb_logcat_invocation(
            serial,
            lines=25,
            limits=limits,
            scheduler_resources=(),
        )
        legacy = AdbClient(
            runner,
            resolver=lambda name: "/opt/android/platform-tools/adb"
            if name == "adb"
            else None,
        ).logcat(serial, lines=25, limits=limits)
        projected = adb_logcat_result(invocation, legacy)

        self.assertEqual(projected.payload, legacy.as_dict())
        self.assertEqual(projected.tool, ADB_LOGCAT_TOOL)
        self.assertEqual(projected.tool.effect, SemanticEffect.ACTIVE_READ)
        self.assertEqual(
            projected.tool.authority,
            AuthorityCeiling.FIXED_REMOTE_DEVICE_EXEC,
        )
        self.assertEqual(invocation.arguments, {"lines": 25})
        self.assertEqual(invocation.scheduler_resources, ())
        self.assertEqual(projected.target.name, serial)
        self.assertEqual(projected.target.locator.kind, "adb_serial")
        self.assertEqual(projected.target.locator.attributes, {"serial": serial})
        self.assertEqual(projected.target.identity_evidence, ())
        self.assertEqual(projected.payload["line_count"], 2)
        self.assertEqual(
            runner.commands,
            [
                (
                    "/opt/android/platform-tools/adb",
                    "-s",
                    serial,
                    "get-state",
                ),
                (
                    "/opt/android/platform-tools/adb",
                    "-s",
                    serial,
                    "logcat",
                    "-d",
                    "-t",
                    "25",
                ),
            ],
        )

    def test_adb_push_keeps_stable_identity_separate_from_locator_and_resources(
        self,
    ) -> None:
        limits = ExecutionLimits(timeout_seconds=8.0)
        invocation = adb_push_invocation(
            "phone-s22",
            "192.0.2.100:38871",
            Path("/tmp/firmware.bin"),
            "/sdcard/firmware.bin",
            replace=False,
            max_bytes=2048,
            limits=limits,
            scheduler_resources=("device:phone-s22",),
        )
        legacy = AdbTransferResult(
            direction="push",
            serial="192.0.2.100:38871",
            source="/tmp/firmware.bin",
            destination="/sdcard/firmware.bin",
            size_bytes=18,
            sha256="a" * 64,
            replaced_existing=False,
        )
        projected = adb_push_result(invocation, legacy)
        self.assertEqual(projected.payload, legacy.as_dict())
        self.assertEqual(projected.tool, ADB_PUSH_TOOL)
        self.assertEqual(projected.tool.effect, SemanticEffect.MUTATION)
        self.assertEqual(
            projected.tool.authority,
            AuthorityCeiling.FIXED_REMOTE_DEVICE_EXEC,
        )
        self.assertIs(invocation.execution_limits, limits)
        self.assertEqual(invocation.scheduler_resources, ("device:phone-s22",))
        self.assertEqual(projected.target.name, "phone-s22")
        self.assertEqual(
            projected.target.locator.attributes,
            {"serial": "192.0.2.100:38871"},
        )
        self.assertEqual(projected.artifacts[0].sha256, legacy.sha256)
        self.assertEqual(projected.artifacts[0].path, legacy.destination)
        mismatched = AdbTransferResult(
            direction="push",
            serial="different",
            source=legacy.source,
            destination=legacy.destination,
            size_bytes=legacy.size_bytes,
            sha256=legacy.sha256,
            replaced_existing=False,
        )
        with self.assertRaisesRegex(ValueError, "serial does not match"):
            adb_push_result(invocation, mismatched)
        with self.assertRaisesRegex(ValueError, "requires a push"):
            adb_push_result(
                invocation,
                AdbTransferResult(
                    direction="pull",
                    serial=legacy.serial,
                    source=legacy.source,
                    destination=legacy.destination,
                    size_bytes=legacy.size_bytes,
                    sha256=legacy.sha256,
                    replaced_existing=False,
                ),
            )

        failure = AdbTransferError(
            "remote commit ambiguous",
            action_attempted=True,
            committed=False,
            cleanup_failed=True,
        )
        rejected = adb_push_error(invocation, failure)
        self.assertFalse(rejected.ok)
        self.assertEqual(rejected.error.code, "adb_transfer_failed")
        self.assertTrue(rejected.partial_effect.action_attempted)
        self.assertFalse(rejected.partial_effect.committed)
        self.assertTrue(rejected.partial_effect.cleanup_failed)

    def test_adb_pull_preserves_local_commit_and_cleanup_evidence(self) -> None:
        limits = ExecutionLimits(timeout_seconds=8.0)
        invocation = adb_pull_invocation(
            "phone-s22",
            "USB-SERIAL-123",
            "/sdcard/report.bin",
            Path("/tmp/report.bin"),
            replace=True,
            max_bytes=4096,
            limits=limits,
            scheduler_resources=(),
        )
        legacy = AdbTransferResult(
            direction="pull",
            serial="USB-SERIAL-123",
            source="/sdcard/report.bin",
            destination="/tmp/report.bin",
            size_bytes=18,
            sha256="b" * 64,
            replaced_existing=True,
            local_directory_synced=True,
        )
        projected = adb_pull_result(invocation, legacy)
        self.assertEqual(projected.payload, legacy.as_dict())
        self.assertEqual(projected.tool, ADB_PULL_TOOL)
        self.assertEqual(projected.tool.effect, SemanticEffect.MUTATION)
        self.assertEqual(
            projected.tool.authority,
            AuthorityCeiling.FIXED_REMOTE_DEVICE_EXEC,
        )
        self.assertIs(invocation.execution_limits, limits)
        self.assertEqual(invocation.scheduler_resources, ())
        self.assertEqual(projected.artifacts[0].path, legacy.destination)
        self.assertEqual(projected.artifacts[0].sha256, legacy.sha256)
        self.assertEqual(projected.target.name, "phone-s22")
        self.assertEqual(
            projected.target.locator.attributes,
            {"serial": "USB-SERIAL-123"},
        )
        failure = AdbTransferError(
            "cleanup failed after local commit",
            action_attempted=True,
            committed=True,
            cleanup_failed=True,
        )
        rejected = adb_pull_error(invocation, failure)
        self.assertFalse(rejected.ok)
        self.assertEqual(rejected.error.code, "adb_transfer_failed")
        self.assertTrue(rejected.partial_effect.action_attempted)
        self.assertTrue(rejected.partial_effect.committed)
        self.assertTrue(rejected.partial_effect.cleanup_failed)

    def test_ssh_unknown_commit_preserves_process_and_nullable_effect(self) -> None:
        invocation = ssh_push_invocation(
            _target(),
            Path("/tmp/unused-source.bin"),
            "/remote/target.bin",
            replace=False,
            max_bytes=4096,
            limits=ExecutionLimits(timeout_seconds=7.0),
            scheduler_resources=(),
        )
        process = ProcessResult(
            state=ProcessState.TIMED_OUT,
            exit_code=None,
            stdout="",
            stderr="SSH connection lost",
            duration_seconds=7.0,
            error="process timed out",
        )
        failure = SshTransferError(
            "response lost after remote commit attempt",
            action_attempted=True,
            commit_uncertain=True,
            code="ssh_commit_uncertain",
            phase="remote_commit",
            process=process,
        )
        projected = ssh_push_error(invocation, failure)
        self.assertFalse(projected.ok)
        self.assertEqual(projected.error.code, "ssh_commit_uncertain")
        self.assertIs(projected.process, process)
        self.assertTrue(projected.partial_effect.action_attempted)
        self.assertIsNone(projected.partial_effect.committed)
        self.assertEqual(projected.partial_effect.stage, "remote_commit")

    def test_ssh_pull_preserves_success_payload_and_partial_effect_failure(
        self,
    ) -> None:
        limits = ExecutionLimits(timeout_seconds=9.0)
        target = _target()
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "result.bin"
            invocation = ssh_pull_invocation(
                target,
                "/remote/result.bin",
                destination,
                replace=False,
                max_bytes=4096,
                limits=limits,
                scheduler_resources=(),
            )
            legacy = SshTransferResult(
                direction="pull",
                target="phone",
                source="/remote/result.bin",
                destination=str(destination),
                size_bytes=12,
                sha256="a" * 64,
                replaced_existing=False,
                local_directory_synced=True,
            )
            projected = ssh_pull_result(invocation, legacy)

        self.assertEqual(projected.payload, legacy.as_dict())
        self.assertEqual(projected.tool, SSH_PULL_TOOL)
        self.assertEqual(projected.tool.effect, SemanticEffect.MUTATION)
        self.assertEqual(
            projected.tool.authority,
            AuthorityCeiling.FIXED_REMOTE_DEVICE_EXEC,
        )
        self.assertEqual(invocation.scheduler_resources, ())
        self.assertEqual(projected.artifacts[0].path, legacy.destination)
        self.assertEqual(projected.artifacts[0].sha256, legacy.sha256)

        failure = SshTransferError(
            "local staging cleanup failed after SSH pull",
            action_attempted=True,
            committed=True,
            cleanup_failed=True,
        )
        failed = ssh_pull_error(invocation, failure)
        self.assertFalse(failed.ok)
        self.assertTrue(failed.partial_effect.action_attempted)
        self.assertTrue(failed.partial_effect.committed)
        self.assertTrue(failed.partial_effect.cleanup_failed)
        self.assertEqual(failed.error.code, "ssh_transfer_failed")

    def test_artifact_inspect_projects_real_production_result_without_legacy_drift(
        self,
    ) -> None:
        limits = ExecutionLimits(timeout_seconds=5.0)
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "artifact.bin"
            source.write_bytes(b"artifact-data")
            invocation = artifact_inspect_invocation(
                source,
                max_bytes=1024,
                limits=limits,
                scheduler_resources=(),
            )
            legacy = LocalArtifactInspector().inspect(
                source,
                max_bytes=1024,
                limits=limits,
            )
            projected = artifact_inspect_result(invocation, legacy)

        self.assertEqual(projected.payload, legacy.as_dict())
        self.assertEqual(projected.tool, ARTIFACT_INSPECT_TOOL)
        self.assertEqual(projected.tool.effect, SemanticEffect.PASSIVE_READ)
        self.assertEqual(projected.tool.authority, AuthorityCeiling.NONE)
        self.assertEqual(invocation.scheduler_resources, ())
        self.assertEqual(projected.artifacts[0].sha256, legacy.sha256)
        self.assertEqual(
            projected.target.identity_evidence[0].attributes["real_path"],
            str(legacy.real_path),
        )

    def test_artifact_deploy_projects_verified_commit_without_legacy_drift(self) -> None:
        limits = ExecutionLimits(timeout_seconds=5.0)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.bin"
            source.write_bytes(b"artifact-data")
            destination_directory = root / "out"
            destination_directory.mkdir()
            invocation = artifact_deploy_invocation(
                source,
                destination_directory,
                destination_name="firmware.bin",
                replace=False,
                max_bytes=1024,
                limits=limits,
                scheduler_resources=(),
            )
            legacy = LocalArtifactDeployer().deploy(
                source,
                destination_directory,
                destination_name="firmware.bin",
                max_bytes=1024,
                limits=limits,
            )
            projected = artifact_deploy_result(invocation, legacy)
            self.assertEqual((destination_directory / "firmware.bin").read_bytes(), b"artifact-data")

        self.assertEqual(projected.payload, legacy.as_dict())
        self.assertEqual(projected.tool, ARTIFACT_DEPLOY_TOOL)
        self.assertEqual(projected.tool.effect, SemanticEffect.MUTATION)
        self.assertEqual(projected.tool.authority, AuthorityCeiling.NONE)
        self.assertIs(invocation.execution_limits, limits)
        self.assertEqual(invocation.scheduler_resources, ())
        self.assertEqual(invocation.arguments["destination_name"], "firmware.bin")
        self.assertEqual(projected.target.name, str(destination_directory))
        self.assertEqual(projected.artifacts[0].path, str(legacy.destination))
        self.assertEqual(projected.artifacts[0].sha256, legacy.sha256)
        with self.assertRaisesRegex(ValueError, "tool invocation identity mismatch"):
            artifact_deploy_result(
                artifact_inspect_invocation(
                    source, max_bytes=1024, limits=limits, scheduler_resources=()
                ),
                legacy,
            )

    def test_artifact_deploy_error_retains_known_commit_and_unknown_cleanup(self) -> None:
        limits = ExecutionLimits(timeout_seconds=5.0)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.bin"
            source.write_bytes(b"payload")
            destination_directory = root / "out"
            destination_directory.mkdir()
            invocation = artifact_deploy_invocation(
                source,
                destination_directory,
                destination_name=None,
                replace=False,
                max_bytes=1024,
                limits=limits,
                scheduler_resources=(),
            )
            with patch(
                "local_agent.host_ops.capabilities.local.files.deploy._sync_directory",
                side_effect=ArtifactOperationTimeout,
            ):
                with self.assertRaises(ArtifactDeploymentError) as captured:
                    LocalArtifactDeployer().deploy(
                        source, destination_directory, max_bytes=1024, limits=limits
                    )
            self.assertTrue((destination_directory / "source.bin").exists())
            rejected = artifact_deploy_error(invocation, captured.exception)

        self.assertFalse(rejected.ok)
        self.assertEqual(rejected.error.code, "artifact_deployment_failed")
        self.assertEqual(rejected.error.message, str(captured.exception))
        self.assertTrue(rejected.partial_effect.committed)
        self.assertTrue(rejected.partial_effect.action_attempted)
        self.assertIsNone(rejected.partial_effect.cleanup_failed)

    def test_artifact_deploy_precommit_error_does_not_invent_attempt(self) -> None:
        limits = ExecutionLimits(timeout_seconds=5.0)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.bin"
            source.write_bytes(b"new")
            destination_directory = root / "out"
            destination_directory.mkdir()
            (destination_directory / "source.bin").write_bytes(b"original")
            invocation = artifact_deploy_invocation(
                source,
                destination_directory,
                destination_name=None,
                replace=False,
                max_bytes=1024,
                limits=limits,
                scheduler_resources=(),
            )
            with self.assertRaises(ArtifactDeploymentError) as captured:
                LocalArtifactDeployer().deploy(
                    source, destination_directory, max_bytes=1024, limits=limits
                )
            projected = artifact_deploy_error(invocation, captured.exception)
            self.assertEqual((destination_directory / "source.bin").read_bytes(), b"original")

        self.assertFalse(projected.ok)
        self.assertFalse(projected.partial_effect.committed)
        self.assertIsNone(projected.partial_effect.action_attempted)
        self.assertIsNone(projected.partial_effect.cleanup_failed)


    def test_ssh_check_projects_real_client_result_and_keeps_target_out_of_resources(
        self,
    ) -> None:
        limits = ExecutionLimits(timeout_seconds=7.0)
        runner = _Runner(_completed("u0_a123\n"))
        target = _target()
        invocation = ssh_check_invocation(
            target,
            limits=limits,
            scheduler_resources=(),
        )
        legacy = SshClient(runner).check(target, limits=limits)
        projected = ssh_check_result(invocation, legacy)

        self.assertEqual(projected.payload, legacy.as_dict())
        self.assertIs(projected.process, legacy.process)
        self.assertTrue(projected.ok)
        self.assertEqual(projected.tool, SSH_CHECK_TOOL)
        self.assertEqual(projected.tool.effect, SemanticEffect.ACTIVE_READ)
        self.assertEqual(
            projected.tool.authority,
            AuthorityCeiling.FIXED_REMOTE_DEVICE_EXEC,
        )
        self.assertEqual(invocation.scheduler_resources, ())
        self.assertEqual(projected.target.name, "phone")
        self.assertEqual(
            projected.target.locator.attributes,
            {"host": "example.test", "port": 8022},
        )
        identity = projected.target.identity_evidence[0]
        self.assertEqual(identity.kind, "ssh_remote_user")
        self.assertEqual(identity.attributes["remote_user"], "u0_a123")
        self.assertTrue(identity.attributes["identity_matches"])

    def test_ssh_push_ambiguous_commit_failure_preserves_existing_partial_effect_evidence(
        self,
    ) -> None:
        limits = ExecutionLimits(timeout_seconds=5.0)
        target = _target()
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "firmware.bin"
            payload = b"firmware-data"
            source.write_bytes(payload)
            digest = hashlib.sha256(payload).hexdigest()
            invocation = ssh_push_invocation(
                target,
                source,
                "/remote/firmware.bin",
                replace=False,
                max_bytes=1024,
                limits=limits,
                scheduler_resources=(),
            )
            transfer = SshFileTransfer(_Runner())
            with (
                patch.object(transfer, "_require_remote_directory"),
                patch.object(
                    transfer,
                    "_validate_remote_destination",
                    return_value=False,
                ),
                patch.object(
                    transfer,
                    "_remote_size",
                    return_value=len(payload),
                ),
                patch.object(
                    transfer,
                    "_remote_sha256",
                    return_value=digest,
                ),
                patch(
                    "local_agent.host_ops.capabilities.remote.ssh.transfer."
                    "_commit_remote_no_clobber",
                    side_effect=SshTransferError(
                        "commit timed out",
                        action_attempted=True,
                    ),
                ),
                patch(
                    "local_agent.host_ops.capabilities.remote.ssh.transfer."
                    "_cleanup_remote_stage",
                    return_value=True,
                ),
            ):
                with self.assertRaisesRegex(SshTransferError, "commit timed out") as caught:
                    transfer.push(
                        target,
                        source,
                        "/remote/firmware.bin",
                        max_bytes=1024,
                        limits=limits,
                    )

        projected = ssh_push_error(invocation, caught.exception)
        self.assertFalse(projected.ok)
        self.assertEqual(projected.tool, SSH_PUSH_TOOL)
        self.assertEqual(projected.tool.effect, SemanticEffect.MUTATION)
        self.assertEqual(
            projected.tool.authority,
            AuthorityCeiling.FIXED_REMOTE_DEVICE_EXEC,
        )
        self.assertEqual(invocation.scheduler_resources, ())
        self.assertTrue(projected.partial_effect.action_attempted)
        self.assertFalse(projected.partial_effect.committed)
        self.assertFalse(projected.partial_effect.cleanup_failed)
        self.assertEqual(projected.error.code, "ssh_transfer_failed")
        self.assertEqual(projected.error.message, "commit timed out")


if __name__ == "__main__":
    unittest.main()
