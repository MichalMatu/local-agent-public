"""CLI surface for deterministic remote Git workspaces."""

from __future__ import annotations

import json
import sys
from collections.abc import Sequence
from pathlib import Path

from local_agent.host_ops.capabilities.local.git import GitRepositoryContext, LocalGitClient, LocalGitError
from local_agent.host_ops.core.execution import ExecutionLimits
from local_agent.host_ops.workflows.remote_git import (
    RemoteGitCacheError,
    RemoteGitCacheManager,
    RemoteGitRunner,
    RemoteGitWorkspace,
    derive_workspace_name,
)

from ..host_target import HostTargetResolutionError, load_host_target
from ..process_output import emit_process_output, process_exit_code

_LOCAL_GIT_TIMEOUT_SECONDS = 30.0
_CACHE_EVIDENCE_BYTES = 256 * 1024


def run_prepare(
    alias: str,
    *,
    repository_url: str,
    revision: str,
    workspace_name: str,
    lock: str | None,
    clean_mode: str,
    timeout_seconds: float,
    as_json: bool,
) -> int:
    workspace = RemoteGitWorkspace(
        repository_url=repository_url,
        revision=revision,
        workspace=workspace_name,
        lock=lock,
        clean_mode=clean_mode,
    )
    return _run_workspace(
        alias,
        workspace,
        timeout_seconds=timeout_seconds,
        as_json=as_json,
        remote_argv=None,
    )


def run_command(
    alias: str,
    remote_argv: Sequence[str],
    *,
    repository_url: str,
    revision: str,
    workspace_name: str,
    lock: str | None,
    clean_mode: str,
    timeout_seconds: float,
    as_json: bool,
) -> int:
    workspace = RemoteGitWorkspace(
        repository_url=repository_url,
        revision=revision,
        workspace=workspace_name,
        lock=lock,
        clean_mode=clean_mode,
    )
    return _run_workspace(
        alias,
        workspace,
        timeout_seconds=timeout_seconds,
        as_json=as_json,
        remote_argv=remote_argv,
    )


def run_prepare_current(
    alias: str,
    *,
    source_path: str,
    remote_name: str,
    repository_url_override: str | None,
    workspace_name: str | None,
    lock: str | None,
    clean_mode: str,
    timeout_seconds: float,
    as_json: bool,
) -> int:
    try:
        context, workspace = _current_workspace(
            source_path=source_path,
            remote_name=remote_name,
            repository_url_override=repository_url_override,
            workspace_name=workspace_name,
            lock=lock,
            clean_mode=clean_mode,
        )
    except (LocalGitError, ValueError) as exc:
        _emit_input_error(str(exc), as_json=as_json)
        return 2
    return _run_workspace(
        alias,
        workspace,
        timeout_seconds=timeout_seconds,
        as_json=as_json,
        remote_argv=None,
        local_context=context,
    )


def run_current(
    alias: str,
    remote_argv: Sequence[str],
    *,
    source_path: str,
    remote_name: str,
    repository_url_override: str | None,
    workspace_name: str | None,
    lock: str | None,
    clean_mode: str,
    timeout_seconds: float,
    as_json: bool,
) -> int:
    try:
        context, workspace = _current_workspace(
            source_path=source_path,
            remote_name=remote_name,
            repository_url_override=repository_url_override,
            workspace_name=workspace_name,
            lock=lock,
            clean_mode=clean_mode,
        )
    except (LocalGitError, ValueError) as exc:
        _emit_input_error(str(exc), as_json=as_json)
        return 2
    return _run_workspace(
        alias,
        workspace,
        timeout_seconds=timeout_seconds,
        as_json=as_json,
        remote_argv=remote_argv,
        local_context=context,
    )


def run_cache_list(alias: str, *, timeout_seconds: float, as_json: bool) -> int:
    try:
        target = load_host_target(alias)
        result = RemoteGitCacheManager().list(target, limits=_cache_limits(timeout_seconds))
    except (HostTargetResolutionError, ValueError) as exc:
        _emit_input_error(str(exc), as_json=as_json)
        return 2
    except RemoteGitCacheError as exc:
        _emit_cache_error(str(exc), as_json=as_json)
        return 1

    if as_json:
        print(json.dumps({"target": alias, **result.as_dict()}, sort_keys=True))
    elif result.process.ok:
        if not result.entries:
            print("No remote Git cache workspaces found")
        for entry in result.entries:
            fields = [f"workspace={entry.workspace}", f"state={entry.state}"]
            if entry.repository_url is not None:
                fields.append(f"repository_url={entry.repository_url}")
            if entry.revision is not None:
                fields.append(f"revision={entry.revision}")
            print(" ".join(fields))
    else:
        emit_process_output(result.process)
    return process_exit_code(result.process)


def run_cache_remove(
    alias: str,
    workspace_name: str,
    *,
    timeout_seconds: float,
    as_json: bool,
) -> int:
    try:
        target = load_host_target(alias)
        result = RemoteGitCacheManager().remove(
            target,
            workspace_name,
            limits=_cache_limits(timeout_seconds),
        )
    except (HostTargetResolutionError, ValueError) as exc:
        _emit_input_error(str(exc), as_json=as_json)
        return 2
    except RemoteGitCacheError as exc:
        _emit_cache_error(str(exc), as_json=as_json)
        return 1

    if as_json:
        print(json.dumps({"target": alias, **result.as_dict()}, sort_keys=True))
    elif result.process.ok:
        status = "removed" if result.existed else "already absent"
        print(f"remote Git cache workspace {workspace_name}: {status}")
    else:
        emit_process_output(result.process)
    return process_exit_code(result.process)


def _current_workspace(
    *,
    source_path: str,
    remote_name: str,
    repository_url_override: str | None,
    workspace_name: str | None,
    lock: str | None,
    clean_mode: str,
) -> tuple[GitRepositoryContext, RemoteGitWorkspace]:
    context = LocalGitClient().inspect(
        Path(source_path),
        remote_name=remote_name,
        require_clean=True,
        limits=ExecutionLimits(timeout_seconds=_LOCAL_GIT_TIMEOUT_SECONDS),
    )
    effective_repository_url = repository_url_override or context.remote_url
    workspace = RemoteGitWorkspace(
        repository_url=effective_repository_url,
        revision=context.revision,
        workspace=workspace_name or derive_workspace_name(effective_repository_url),
        lock=lock,
        clean_mode=clean_mode,
    )
    return context, workspace


def _run_workspace(
    alias: str,
    workspace: RemoteGitWorkspace,
    *,
    timeout_seconds: float,
    as_json: bool,
    remote_argv: Sequence[str] | None,
    local_context: GitRepositoryContext | None = None,
) -> int:
    try:
        target = load_host_target(alias)
        limits = ExecutionLimits(timeout_seconds=timeout_seconds)
        runner = RemoteGitRunner()
        if remote_argv is None:
            result = runner.prepare(target, workspace, limits=limits)
        else:
            result = runner.run(target, workspace, remote_argv, limits=limits)
    except (HostTargetResolutionError, ValueError) as exc:
        _emit_input_error(str(exc), as_json=as_json)
        return 2

    if as_json:
        payload = {"target": alias, **result.as_dict()}
        if local_context is not None:
            payload["local_repository"] = local_context.as_dict()
        print(json.dumps(payload, sort_keys=True))
    else:
        emit_process_output(result.process)
        if result.process.ok and not result.prepared:
            print(
                "hostops: remote Git workflow finished without readiness evidence",
                file=sys.stderr,
            )

    if result.process.ok and not result.prepared:
        return 1
    return process_exit_code(result.process)


def _cache_limits(timeout_seconds: float) -> ExecutionLimits:
    return ExecutionLimits(
        timeout_seconds=timeout_seconds,
        max_stdout_bytes=_CACHE_EVIDENCE_BYTES,
        max_stderr_bytes=64 * 1024,
    )


def _emit_input_error(message: str, *, as_json: bool) -> None:
    if as_json:
        print(json.dumps({"ok": False, "error": message}, sort_keys=True))
    else:
        print(f"hostops: {message}", file=sys.stderr)


def _emit_cache_error(message: str, *, as_json: bool) -> None:
    if as_json:
        print(json.dumps({"ok": False, "error": message}, sort_keys=True), file=sys.stderr)
    else:
        print(f"remote Git cache failed: {message}", file=sys.stderr)
