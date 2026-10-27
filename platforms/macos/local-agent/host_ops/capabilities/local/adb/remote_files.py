"""Fixed remote-file command protocol for verified ADB transfer."""

from __future__ import annotations

import re
from collections.abc import Callable
from pathlib import Path, PurePosixPath

from local_agent.host_ops.core.execution import ExecutionLimits, ProcessResult, ProcessRunner, ProcessState

_REMOTE_PATH_SEGMENT = re.compile(r"^[A-Za-z0-9._@%+=,-]+$")
_SHA256_PATTERN = re.compile(r"(?i)(?<![0-9a-f])[0-9a-f]{64}(?![0-9a-f])")
_REMOTE_HASH_COMMANDS = (
    ("sha256sum",),
    ("toybox", "sha256sum"),
    ("openssl", "dgst", "-sha256"),
)


class AdbTransferError(RuntimeError):
    """Raised when verified ADB transfer cannot complete safely."""

    def __init__(
        self,
        message: str,
        *,
        action_attempted: bool = False,
        committed: bool = False,
        cleanup_failed: bool = False,
    ) -> None:
        super().__init__(message)
        self.action_attempted = action_attempted or committed
        self.committed = committed
        self.cleanup_failed = cleanup_failed


class AdbRemoteFiles:
    """Execute the fixed ADB command set needed for one file transfer."""

    def __init__(
        self,
        runner: ProcessRunner,
        executable: str,
        serial: str,
        limit_provider: Callable[[], ExecutionLimits],
    ) -> None:
        self._runner = runner
        self._executable = executable
        self._serial = serial
        self._limits = limit_provider

    def require_ready(self) -> None:
        result = self._runner.run(
            (self._executable, "-s", self._serial, "get-state"),
            limits=self._limits(),
        )
        self._require_process_ok(result, f"check ADB device {self._serial}")
        if result.stdout.strip() != "device":
            raise AdbTransferError(f"ADB device {self._serial} is not ready")

    def push(self, source: Path, destination: str) -> None:
        self._require_process_ok(
            self._runner.run(
                (self._executable, "-s", self._serial, "push", str(source), destination),
                limits=self._limits(),
            ),
            "ADB push",
        )

    def pull(self, source: str, destination: Path) -> None:
        self._require_process_ok(
            self._runner.run(
                (self._executable, "-s", self._serial, "pull", source, str(destination)),
                limits=self._limits(),
            ),
            "ADB pull",
        )

    def require_directory(self, path: str) -> None:
        if self._test("-L", path):
            raise AdbTransferError("remote destination directory must not be a symbolic link")
        if not self._test("-d", path):
            raise AdbTransferError("remote destination directory does not exist")

    def validate_destination(self, path: str, *, replace: bool) -> bool:
        symlink = self._test("-L", path)
        exists = symlink or self._test("-e", path)
        if not exists:
            return False
        if symlink:
            raise AdbTransferError("remote destination must not be a symbolic link")
        if not self._test("-f", path):
            raise AdbTransferError("remote destination exists and is not a regular file")
        if not replace:
            raise AdbTransferError("remote destination exists; explicit replace intent is required")
        return True

    def require_regular_file(self, path: str, *, label: str) -> None:
        if self._test("-L", path):
            raise AdbTransferError(f"remote {label} must not be a symbolic link")
        if not self._test("-f", path):
            raise AdbTransferError(f"remote {label} is not a regular file")

    def size(self, path: str) -> int:
        result = self._shell(("wc", "-c", path), "remote size")
        try:
            size = int(result.stdout.strip().split()[0])
        except (IndexError, ValueError) as exc:
            raise AdbTransferError("remote size command returned invalid output") from exc
        if size < 0:
            raise AdbTransferError("remote size command returned a negative size")
        return size

    def sha256(self, path: str) -> str:
        for command in _REMOTE_HASH_COMMANDS:
            result = self._runner.run(
                (self._executable, "-s", self._serial, "shell", *command, path),
                limits=self._limits(),
            )
            if result.stdout_truncated or result.stderr_truncated:
                raise AdbTransferError("remote SHA-256: command output was truncated")
            if result.state is not ProcessState.COMPLETED:
                raise AdbTransferError(_process_failure("remote SHA-256", result))
            if result.exit_code == 127:
                continue
            if result.exit_code != 0:
                raise AdbTransferError(_process_failure("remote SHA-256", result))
            match = _SHA256_PATTERN.search(result.stdout)
            if match is None:
                raise AdbTransferError("remote SHA-256 command returned invalid output")
            return match.group(0).lower()
        raise AdbTransferError("remote SHA-256: no supported hash command is available")

    def commit_stage(self, stage: str, destination: str, *, replace: bool) -> None:
        move_flag = "-fT" if replace else "-nT"
        self._shell(("mv", move_flag, stage, destination), "remote ADB commit")
        if not self._test("-e", stage):
            return
        if replace:
            raise AdbTransferError("remote ADB commit left staging file behind")
        raise AdbTransferError("remote destination appeared during no-clobber commit")

    def cleanup(self, stage: str, *, best_effort: bool = False) -> bool:
        try:
            result = self._runner.run(
                (self._executable, "-s", self._serial, "shell", "rm", "-f", stage),
                limits=self._limits(),
            )
        except AdbTransferError:
            if best_effort:
                return False
            raise
        if result.ok:
            return True
        if best_effort:
            return False
        raise AdbTransferError(_process_failure("remote staging cleanup", result))

    def _test(self, operator: str, path: str) -> bool:
        result = self._runner.run(
            (self._executable, "-s", self._serial, "shell", "test", operator, path),
            limits=self._limits(),
        )
        if result.state is not ProcessState.COMPLETED:
            raise AdbTransferError(_process_failure("remote test", result))
        if result.exit_code == 0:
            return True
        if result.exit_code == 1 and not result.stdout.strip() and not result.stderr.strip():
            return False
        raise AdbTransferError(_process_failure("remote test", result))

    def _shell(self, argv: tuple[str, ...], context: str) -> ProcessResult:
        result = self._runner.run(
            (self._executable, "-s", self._serial, "shell", *argv),
            limits=self._limits(),
        )
        self._require_process_ok(result, context)
        return result

    @staticmethod
    def _require_process_ok(result: ProcessResult, context: str) -> None:
        if result.ok and not result.stdout_truncated and not result.stderr_truncated:
            return
        if result.stdout_truncated or result.stderr_truncated:
            raise AdbTransferError(f"{context}: command output was truncated")
        raise AdbTransferError(_process_failure(context, result))


def normalize_remote_file_path(remote_path: str) -> str:
    if (
        not isinstance(remote_path, str)
        or not remote_path.startswith("/")
        or remote_path.startswith("//")
    ):
        raise AdbTransferError("remote path must be an absolute POSIX path")
    if len(remote_path) > 4096 or "\x00" in remote_path:
        raise AdbTransferError("remote path is too long or contains a NUL byte")
    path = PurePosixPath(remote_path)
    if str(path) != remote_path or remote_path == "/":
        raise AdbTransferError("remote path must be normalized and identify a file path")
    for segment in path.parts[1:]:
        if segment in {"", ".", ".."} or not _REMOTE_PATH_SEGMENT.fullmatch(segment):
            raise AdbTransferError(
                "remote path contains unsupported characters or traversal segments"
            )
    return remote_path


def _process_failure(context: str, result: ProcessResult) -> str:
    detail = result.stderr.strip() or result.stdout.strip() or result.error
    if not detail:
        detail = f"state={result.state.value} exit_code={result.exit_code}"
    return f"{context}: {detail}"
