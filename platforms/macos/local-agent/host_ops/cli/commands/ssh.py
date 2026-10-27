"""CLI surface for the deterministic SSH capability."""

from __future__ import annotations

import json
import sys
from collections.abc import Sequence
from pathlib import Path

from local_agent.host_ops.capabilities.remote.ssh import (
    DEFAULT_MAX_TRANSFER_BYTES as _CAPABILITY_DEFAULT_MAX_TRANSFER_BYTES,
)
from local_agent.host_ops.capabilities.remote.ssh import (
    SshClient,
    SshFileTransfer,
    SshTransferError,
)
from local_agent.host_ops.core.config import HostTarget
from local_agent.host_ops.core.execution import ExecutionLimits

from ..host_target import HostTargetResolutionError, load_host_target
from ..process_output import emit_process_output, process_exit_code

DEFAULT_MAX_TRANSFER_BYTES = _CAPABILITY_DEFAULT_MAX_TRANSFER_BYTES
_TRANSFER_EVIDENCE_BYTES = 64 * 1024


def run_exec(
    alias: str,
    remote_argv: Sequence[str],
    *,
    timeout_seconds: float,
    as_json: bool,
) -> int:
    target = _load_target(alias, as_json=as_json)
    if target is None:
        return 2

    try:
        limits = ExecutionLimits(timeout_seconds=timeout_seconds)
        result = SshClient().execute(target, remote_argv, limits=limits)
    except ValueError as exc:
        _emit_input_error(str(exc), as_json=as_json)
        return 2

    if as_json:
        payload = {"target": alias, **result.as_dict()}
        print(json.dumps(payload, sort_keys=True))
    else:
        emit_process_output(result)

    return process_exit_code(result)


def run_check(alias: str, *, timeout_seconds: float, as_json: bool) -> int:
    target = _load_target(alias, as_json=as_json)
    if target is None:
        return 2

    try:
        limits = ExecutionLimits(timeout_seconds=timeout_seconds)
        result = SshClient().check(target, limits=limits)
    except ValueError as exc:
        _emit_input_error(str(exc), as_json=as_json)
        return 2

    if as_json:
        payload = {"target": alias, **result.as_dict()}
        print(json.dumps(payload, sort_keys=True))
    elif result.ok:
        print(f"SSH check: PASS ({alias}, user={result.remote_user})")
    else:
        print(f"SSH check: FAIL ({alias})", file=sys.stderr)
        emit_process_output(result.process)
        if result.process.ok and not result.identity_matches:
            print(
                f"remote user mismatch: expected {result.expected_user!r}, "
                f"got {result.remote_user!r}",
                file=sys.stderr,
            )

    if result.ok:
        return 0
    if result.process.ok:
        return 1
    return process_exit_code(result.process)


def run_push(
    alias: str,
    local_source: str,
    remote_destination: str,
    *,
    replace: bool,
    max_bytes: int,
    timeout_seconds: float,
    as_json: bool,
) -> int:
    target = _load_target(alias, as_json=as_json)
    if target is None:
        return 2
    try:
        result = SshFileTransfer().push(
            target,
            Path(local_source),
            remote_destination,
            replace=replace,
            max_bytes=max_bytes,
            limits=_transfer_limits(timeout_seconds),
        )
    except ValueError as exc:
        _emit_input_error(str(exc), as_json=as_json)
        return 2
    except SshTransferError as exc:
        _emit_transfer_error(exc, as_json=as_json)
        return 1
    _emit_transfer_result(result.as_dict(), as_json=as_json)
    return 0


def run_pull(
    alias: str,
    remote_source: str,
    local_destination: str,
    *,
    replace: bool,
    max_bytes: int,
    timeout_seconds: float,
    as_json: bool,
) -> int:
    target = _load_target(alias, as_json=as_json)
    if target is None:
        return 2
    try:
        result = SshFileTransfer().pull(
            target,
            remote_source,
            Path(local_destination),
            replace=replace,
            max_bytes=max_bytes,
            limits=_transfer_limits(timeout_seconds),
        )
    except ValueError as exc:
        _emit_input_error(str(exc), as_json=as_json)
        return 2
    except SshTransferError as exc:
        _emit_transfer_error(exc, as_json=as_json)
        return 1
    _emit_transfer_result(result.as_dict(), as_json=as_json)
    return 0


def _transfer_limits(timeout_seconds: float) -> ExecutionLimits:
    return ExecutionLimits(
        timeout_seconds=timeout_seconds,
        max_stdout_bytes=_TRANSFER_EVIDENCE_BYTES,
        max_stderr_bytes=_TRANSFER_EVIDENCE_BYTES,
    )


def _emit_transfer_result(payload: dict[str, object], *, as_json: bool) -> None:
    if as_json:
        print(json.dumps(payload, sort_keys=True))
        return
    verb = "pushed" if payload["direction"] == "push" else "pulled"
    print(
        f"{verb} {payload['source']} -> {payload['destination']} "
        f"sha256={payload['sha256']} size={payload['size_bytes']}"
    )
    if payload["staging_cleaned"] is False:
        print("warning: remote staging cleanup did not complete", file=sys.stderr)
    if payload["local_directory_synced"] is False:
        print("warning: destination filesystem does not support directory fsync", file=sys.stderr)


def _load_target(alias: str, *, as_json: bool) -> HostTarget | None:
    try:
        return load_host_target(alias)
    except HostTargetResolutionError as exc:
        _emit_input_error(str(exc), as_json=as_json)
        return None


def _emit_input_error(message: str, *, as_json: bool) -> None:
    if as_json:
        print(json.dumps({"ok": False, "error": message}, sort_keys=True))
    else:
        print(f"hostops: {message}", file=sys.stderr)


def _emit_transfer_error(error: SshTransferError, *, as_json: bool) -> None:
    message = str(error)
    if as_json:
        print(
            json.dumps(
                {
                    "ok": False,
                    "error": message,
                    "action_attempted": error.action_attempted,
                    "committed": None if error.commit_uncertain else error.committed,
                    "commit_uncertain": error.commit_uncertain,
                    "cleanup_failed": error.cleanup_failed,
                    "code": error.code,
                    "phase": error.phase,
                    "process": None if error.process is None else error.process.as_dict(),
                },
                sort_keys=True,
            ),
            file=sys.stderr,
        )
    else:
        print(f"SSH transfer failed: {message}", file=sys.stderr)
        if error.commit_uncertain:
            print(
                "warning: remote commit outcome is UNKNOWN; inspect destination before retry",
                file=sys.stderr,
            )
