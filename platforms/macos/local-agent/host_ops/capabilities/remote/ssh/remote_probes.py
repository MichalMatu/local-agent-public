"""Shared remote preflight probes and bounded SSH process evidence."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from local_agent.host_ops.core.config import HostTarget
from local_agent.host_ops.core.execution import ProcessResult, ProcessState

from .client import SshClient
from .errors import SshTransferError, _conflict_error, _input_error

if TYPE_CHECKING:
    from .transfer import _TransferBudget

_SHA256_PATTERN = re.compile(r"(?i)(?<![0-9a-f])[0-9a-f]{64}(?![0-9a-f])")
_REMOTE_HASH_SCRIPT = (
    'if command -v sha256sum >/dev/null 2>&1; then sha256sum "$1"; '
    'elif command -v shasum >/dev/null 2>&1; then shasum -a 256 "$1"; '
    'elif command -v openssl >/dev/null 2>&1; then openssl dgst -sha256 "$1"; '
    "else printf '%s\\n' 'no SHA-256 tool available' >&2; exit 127; fi"
)



class _RemoteProbes:
    _client: SshClient

    def _require_remote_directory(
        self,
        target: HostTarget,
        path: str,
        budget: _TransferBudget,
    ) -> None:
        if self._remote_test(target, "-L", path, budget):
            raise _input_error("remote destination directory must not be a symbolic link")
        if not self._remote_test(target, "-d", path, budget):
            raise _input_error("remote destination directory does not exist")

    def _validate_remote_destination(
        self,
        target: HostTarget,
        path: str,
        *,
        replace: bool,
        budget: _TransferBudget,
    ) -> bool:
        symlink = self._remote_test(target, "-L", path, budget)
        exists = symlink or self._remote_test(target, "-e", path, budget)
        if not exists:
            return False
        if symlink:
            raise _conflict_error("remote destination must not be a symbolic link")
        if not self._remote_test(target, "-f", path, budget):
            raise _conflict_error("remote destination exists and is not a regular file")
        if not replace:
            raise _conflict_error("remote destination exists; explicit replace intent is required")
        return True

    def _require_remote_regular_file(
        self,
        target: HostTarget,
        path: str,
        budget: _TransferBudget,
    ) -> None:
        if self._remote_test(target, "-L", path, budget):
            raise _input_error("remote source must not be a symbolic link")
        if not self._remote_test(target, "-f", path, budget):
            raise _input_error("remote source is not a regular file")

    def _remote_test(
        self,
        target: HostTarget,
        operator: str,
        path: str,
        budget: _TransferBudget,
    ) -> bool:
        return _remote_test(self._client, target, operator, path, budget)

    def _remote_size(self, target: HostTarget, path: str, budget: _TransferBudget) -> int:
        result = self._require_remote_ok(target, ("wc", "-c", path), budget, "remote size")
        try:
            size = int(result.stdout.strip().split()[0])
        except (IndexError, ValueError) as exc:
            raise SshTransferError("remote size command returned invalid output") from exc
        if size < 0:
            raise SshTransferError("remote size command returned a negative size")
        return size

    def _remote_sha256(self, target: HostTarget, path: str, budget: _TransferBudget) -> str:
        result = self._require_remote_ok(
            target,
            ("sh", "-c", _REMOTE_HASH_SCRIPT, "hostops-sha256", path),
            budget,
            "remote SHA-256",
        )
        match = _SHA256_PATTERN.search(result.stdout)
        if match is None:
            raise SshTransferError("remote SHA-256 command returned invalid output")
        return match.group(0).lower()

    def _require_remote_ok(
        self,
        target: HostTarget,
        argv: tuple[str, ...],
        budget: _TransferBudget,
        context: str,
    ) -> ProcessResult:
        result = self._client.execute(target, argv, limits=budget.remaining())
        _require_process_ok(result, context)
        return result


def _require_process_ok(result: ProcessResult, context: str) -> None:
    if result.ok:
        return
    if result.state is ProcessState.OUTPUT_LIMIT_EXCEEDED:
        code = "ssh_size_limit_exceeded"
    elif result.state is ProcessState.OUTPUT_SINK_FAILED:
        code = "ssh_local_io_failed"
    elif result.state is ProcessState.SPAWN_FAILED:
        code = "ssh_process_spawn_failed"
    elif result.state is ProcessState.TIMED_OUT:
        code = "ssh_timeout"
    elif result.exit_code == 255:
        code = "ssh_transport_failed"
    else:
        code = "ssh_remote_command_failed"
    # Read-only SSH probes (test/wc/hash) are not a transfer attempt.
    # SCP upload and the bounded SSH data stream are the only transfer
    # process calls handled here; remote commit tracks its own effects.
    attempted = (
        context in {"SCP upload", "SSH binary download"}
        and result.state is not ProcessState.SPAWN_FAILED
    )
    raise SshTransferError(
        _process_failure(context, result),
        action_attempted=attempted,
        code=code,
        phase="download" if context == "SSH binary download" else "transfer",
        process=result,
    )


def _remote_test(
    client: SshClient,
    target: HostTarget,
    operator: str,
    path: str,
    budget: _TransferBudget,
) -> bool:
    result = client.execute(target, ("test", operator, path), limits=budget.remaining())
    if result.state is not ProcessState.COMPLETED:
        _require_process_ok(result, "remote test")
    if result.exit_code == 0:
        return True
    if result.exit_code == 1:
        return False
    _require_process_ok(result, "remote test")
    raise AssertionError("failed remote test must not return")


def _process_failure(context: str, result: ProcessResult) -> str:
    detail = result.error or result.stderr.strip() or f"exit status {result.exit_code}"
    return f"{context} failed: {detail}"
