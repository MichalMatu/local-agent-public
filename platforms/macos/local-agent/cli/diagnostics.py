#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any

import local_agent.foundation.core as core
import local_agent.foundation.storage as storage
import local_agent.daemon.service as agentd
from local_agent.repository.admin import validate_repository
from local_agent.repository.binding import resolve_execution_target, validate_repository_control_binding
from local_agent.repository.context import RepositoryContext, load_repository_registry
from local_agent.repository.worker import repository_state_dir
from local_agent.operator.local import is_disabled
from local_agent.runtime.health_snapshot import observe_local_health
from local_agent.runtime.backlog_snapshot import observe_local_backlog
from local_agent.cli.host_claims import register_host_claims_parser
from local_agent.cli import register_portable_cli_parsers
from local_agent.runtime.task_preparation import EXECUTION_PROFILES, prepare_task
from local_agent.runtime.task_transport import write_task_bundle
from local_agent.runtime.task_contract import (
    MAX_TASK_FILE_BYTES,
    idle_timeout_for,
    memory_limit_for,
    require_task_agent_binding,
    task_resources_for,
    task_timeout_for,
)
from local_agent.version import RELEASE_VERSION
from local_agent.host_ops.capabilities.local.host import HostProfiler
from local_agent.host_ops.capabilities.local.tools import ToolInspector
from local_agent.tool_runtime.host_inventory import preview_host_inventory
from local_agent.operator.remote import fetch_operator_host_registry
from local_agent.repository.host_mailbox import prepare_host_mailbox_task, write_host_mailbox_bundle
from local_agent.repository.host_mailbox_reader import preview_host_mailbox_snapshot, _trusted_origin
from local_agent.repository.host_publication_preview import preview_host_facts_publication
from local_agent.repository.host_status_reader import preview_host_status_snapshot
from local_agent.repository.host_facts import (
    build_staged_host_facts, initialize_local_host_identity,
    load_local_host_identity, write_staged_host_facts,
)
from local_agent.paths import repository_root
from datetime import datetime, timezone


def print_json(payload: Any) -> None:
    print(json.dumps(payload, indent=2, ensure_ascii=False))


def read_json(path: Path) -> dict[str, Any] | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None


def command_host_inventory(args: argparse.Namespace) -> int:
    """Preview non-authoritative host data without publishing GitHub records."""
    try:
        profile = HostProfiler().inspect()
        inspected = ToolInspector().inspect(args.tools)
        payload = preview_host_inventory(
            host_id=args.host_id,
            source_revision=args.source_revision,
            collected_at=datetime.now(timezone.utc).isoformat(),
            profile=profile,
            tools=inspected,
        )
    except (ValueError, RuntimeError) as exc:
        print_json({"preview": False, "error": f"{type(exc).__name__}: {exc}"})
        return 1
    print_json(payload)
    return 0


def command_host_registry_preview(_args: argparse.Namespace) -> int:
    """Inspect operator-controlled enrollment without enabling routing or execution."""
    try:
        ref, enrollments = fetch_operator_host_registry(repository_root())
    except (ValueError, RuntimeError, OSError, UnicodeError):
        print_json({
            "status": "unverified",
            "source_branch": "operator-control",
            "error": "operator host registry is absent, invalid or unavailable",
            "execution_authority": "not_asserted",
        })
        return 2
    print_json({
        "status": "parsed_preview",
        "source_branch": "operator-control",
        "source_sha": ref,
        "execution_authority": "not_asserted",
        "host_count": len(enrollments),
        "hosts": [
            {"host_id": item.identity.host_id,
             "installation_id": item.identity.installation_id,
             "enabled": item.enabled,
             "repositories": [
                 {"repository_id": repo_id, "agent_binding": binding}
                 for repo_id, binding in item.repositories
             ]}
            for item in enrollments
        ],
    })
    return 0


def command_prepare_host_mailbox(args: argparse.Namespace) -> int:
    """Stage a host-addressed task offline; do not enqueue it or grant authority."""
    try:
        if is_disabled():
            raise ValueError("global operator disabled state prohibits task preparation")
        with Path(args.path).open("rb") as handle:
            raw = handle.read(MAX_TASK_FILE_BYTES + 1)
        if len(raw) > MAX_TASK_FILE_BYTES:
            raise ValueError("task draft exceeds allowed size")
        pinned_sha, enrollments = fetch_operator_host_registry(repository_root())
        if pinned_sha != args.expected_operator_sha:
            raise ValueError("operator registry revision differs from expected SHA")
        task, enrollment, target = prepare_host_mailbox_task(
            json.loads(raw), host_id=args.host_id, repository=args.repository,
            enrollments=enrollments, profile=args.profile, catalog_path=args.catalog,
        )
        registry = load_repository_registry(
            path=args.registry or agentd.multirepo_registry_path(), include_disabled=True,
        )
        protected = [repository_root()]
        for item in registry:
            protected.extend((item.control, item.work, item.checkpoints))
        manifest = write_host_mailbox_bundle(
            args.output_root, task, enrollment=enrollment, target=target,
            protected_paths=protected,
        )
        print_json({
            "status": "staged_only", "execution_authority": "not_asserted",
            "operator_registry_sha": pinned_sha,
            "host_id": enrollment.identity.host_id,
            "repository_id": target.repository_id,
            "manifest": str(manifest),
            "task_digest": agentd.task_digest(task),
            "published": False,
        })
        return 0
    except (ValueError, RuntimeError, OSError, UnicodeError) as exc:
        print_json({"status": "unverified", "published": False, "error": f"{type(exc).__name__}: {exc}"})
        return 2


def command_preview_host_mailbox(args: argparse.Namespace) -> int:
    """Inspect a host task at a pinned Git commit, without claim or execution."""
    try:
        if is_disabled():
            raise ValueError("global operator disabled state blocks host task inspection")
        pinned_operator, enrollments = fetch_operator_host_registry(repository_root())
        if pinned_operator != args.expected_operator_sha:
            raise ValueError("operator registry revision does not match the expected SHA")
        target = resolve_execution_target(args.repository, path=args.catalog)
        matches = [item for item in enrollments if item.identity.host_id == args.host_id]
        if len(matches) != 1:
            raise ValueError("target host enrollment is missing or ambiguous")
        registry = load_repository_registry(path=args.registry or agentd.multirepo_registry_path())
        selected = [item for item in registry if item.repository_id.casefold() == target.repository_id.casefold()]
        if len(selected) != 1:
            raise ValueError("target repository must be enabled in the local registry")
        repository = selected[0]
        if repository.repository.casefold() != target.repository.casefold() or repository.agent_binding != target.agent_binding:
            raise ValueError("local repository identity differs from the canonical catalog")
        validate_repository_control_binding(
            repository_id=repository.repository_id, repository=repository.repository,
            expected_agent_binding=target.agent_binding, control_dir=repository.control,
            catalog_path=args.catalog,
        )
        preview = preview_host_mailbox_snapshot(
            repository.control, host_id=args.host_id, task_id=args.task_id,
            expected_control_sha=args.expected_control_sha, enrollment=matches[0],
            target=target,
        )
        preview["operator_registry_sha"] = pinned_operator
        print_json(preview)
        return 0
    except (ValueError, RuntimeError, OSError, UnicodeError) as exc:
        print_json({"status": "unverified", "claimed": False, "executed": False,
                    "execution_authority": "not_asserted", "error": type(exc).__name__})
        return 2


def command_preview_host_status(args: argparse.Namespace) -> int:
    """Read an immutable Git host heartbeat as evidence, never as execution permission."""
    try:
        pinned_operator, enrollments = fetch_operator_host_registry(repository_root())
        if pinned_operator != args.expected_operator_sha:
            raise ValueError("operator enrollment changed since the expected SHA")
        target = resolve_execution_target(args.repository, path=args.catalog)
        selected_hosts = [item for item in enrollments if item.identity.host_id == args.host_id]
        if len(selected_hosts) != 1:
            raise ValueError("host enrollment is missing or ambiguous")
        registry = load_repository_registry(path=args.registry or agentd.multirepo_registry_path())
        selected = [item for item in registry if item.repository_id.casefold() == target.repository_id.casefold()]
        if (len(selected) != 1 or selected[0].repository.casefold() != target.repository.casefold()
                or selected[0].agent_binding != target.agent_binding):
            raise ValueError("enabled repository binding differs from canonical target")
        validate_repository_control_binding(
            repository_id=selected[0].repository_id, repository=selected[0].repository,
            expected_agent_binding=target.agent_binding, control_dir=selected[0].control,
            catalog_path=args.catalog,
        )
        result = preview_host_status_snapshot(
            selected[0].control, host_id=args.host_id, expected_control_sha=args.expected_control_sha,
            expected_source_sha=args.expected_source_sha, expected_operator_sha=pinned_operator,
            enrollment=selected_hosts[0], target=target, now=datetime.now(timezone.utc),
            max_age_seconds=args.max_age_seconds,
        )
        print_json(result)
        return 0
    except (ValueError, RuntimeError, OSError, UnicodeError) as exc:
        print_json({"status": "unverified", "observed_state": "unknown", "fresh": False,
                    "execution_authority": "not_asserted", "claimed": False,
                    "executed": False, "error": type(exc).__name__})
        return 2


def command_preview_host_facts_publication(args: argparse.Namespace) -> int:
    """Check one immutable host facts candidate against a pinned control ref."""
    try:
        if is_disabled():
            raise ValueError("global disabled state prohibits publication preflight")
        identity = load_local_host_identity(args.identity_file)
        if identity.host_id != args.host_id:
            raise ValueError("selected host does not match local installation")
        pinned_operator, enrollments = fetch_operator_host_registry(repository_root())
        if pinned_operator != args.expected_operator_sha:
            raise ValueError("operator host registry changed since expected SHA")
        target = resolve_execution_target(args.repository, path=args.catalog)
        registry = load_repository_registry(path=args.registry or agentd.multirepo_registry_path())
        matched = [item for item in registry if item.repository_id.casefold() == target.repository_id.casefold()]
        if len(matched) != 1 or matched[0].repository.casefold() != target.repository.casefold() or matched[0].agent_binding != target.agent_binding:
            raise ValueError("repository does not have an enabled canonical local binding")
        validate_repository_control_binding(
            repository_id=matched[0].repository_id, repository=matched[0].repository,
            expected_agent_binding=target.agent_binding, control_dir=matched[0].control,
            catalog_path=args.catalog,
        )
        _trusted_origin(matched[0].control, target)
        preview = preview_host_facts_publication(
            matched[0].control, args.staging_root, identity=identity,
            enrollments=enrollments, repository_id=target.repository_id,
            agent_binding=target.agent_binding, expected_operator_sha=pinned_operator,
            expected_control_sha=args.expected_control_sha,
        )
        print_json(preview)
        return 0
    except (ValueError, RuntimeError, OSError, UnicodeError) as exc:
        print_json({"status": "unverified", "published": False,
                    "heartbeat_published": False, "execution_authority": "not_asserted",
                    "error": type(exc).__name__})
        return 2


def command_init_host_identity(args: argparse.Namespace) -> int:
    """Generate an offline installation ID once, never enroll or register it."""
    try:
        identity = initialize_local_host_identity(args.output_file, args.host_id)
        print_json({"status": "created_not_enrolled", "host_id": identity.host_id,
                    "installation_id": identity.installation_id,
                    "identity_file": str(args.output_file), "published": False,
                    "execution_authority": "not_asserted"})
        return 0
    except (ValueError, RuntimeError, OSError) as exc:
        print_json({"status": "unverified", "published": False,
                    "execution_authority": "not_asserted", "error": type(exc).__name__})
        return 2


def command_stage_host_facts(args: argparse.Namespace) -> int:
    """Create local, unpublished host manifests only after pinned enrollment checks."""
    try:
        if is_disabled():
            raise ValueError("global operator disabled state prohibits host fact staging")
        identity = load_local_host_identity(args.identity_file)
        if identity.host_id != args.host_id:
            raise ValueError("selected host differs from locally configured identity")
        pinned_sha, enrollments = fetch_operator_host_registry(repository_root())
        if pinned_sha != args.expected_operator_sha:
            raise ValueError("operator registry revision differs from expected SHA")
        profile = HostProfiler().inspect()
        inspected = ToolInspector().inspect(args.tools)
        records = build_staged_host_facts(
            identity, enrollments=enrollments, operator_sha=pinned_sha,
            source_revision=args.source_revision,
            collected_at=datetime.now(timezone.utc).isoformat(),
            profile=profile, tools=inspected,
        )
        registry = load_repository_registry(
            path=args.registry or agentd.multirepo_registry_path(), include_disabled=True,
        )
        protected = [repository_root()]
        for item in registry:
            protected.extend((item.control, item.work, item.checkpoints))
        paths = write_staged_host_facts(
            args.output_root, identity, records, protected_paths=protected,
        )
        print_json({
            "status": "staged_only", "published": False,
            "execution_authority": "not_asserted",
            "host_id": identity.host_id,
            "operator_registry_sha": pinned_sha,
            "files": [str(path) for path in paths],
            "heartbeat_published": False,
        })
        return 0
    except (ValueError, RuntimeError, OSError, UnicodeError) as exc:
        print_json({"status": "unverified", "published": False,
                    "execution_authority": "not_asserted", "error": type(exc).__name__})
        return 2


def command_backlog(args: argparse.Namespace) -> int:
    """Inspect local claim/spool record counts without touching task state."""
    payload = observe_local_backlog(
        state_root=args.state_root or agentd.STATE_DIR,
        registry_file=args.registry_file or agentd.multirepo_registry_path(),
    )
    print_json(payload)
    return 0 if payload["status"] == "observed" else 2


def command_health(args: argparse.Namespace) -> int:
    """Fast read-only local health snapshot; no Git traffic or state mutation."""
    try:
        report = observe_local_health(
            status_file=args.status_file or agentd.LOCAL_STATUS_PATH,
            registry_file=args.registry_file or agentd.multirepo_registry_path(),
            max_age_seconds=args.max_age_seconds,
        )
    except (ValueError, OSError):
        report = {
            "schema_version": 1, "status": "unverified",
            "state": "unverified", "warnings": ["invalid_health_request"],
            "execution_authority": "not_asserted",
        }
    print_json(report)
    return {"healthy": 0, "degraded": 1, "unverified": 2}[str(report["status"])]


def command_status(_args: argparse.Namespace) -> int:
    payload = read_json(agentd.LOCAL_STATUS_PATH)
    if payload is None:
        print_json({"state": "unknown", "error": "status file not found"})
        return 1
    print_json(_status_with_liveness(payload))
    return 0


def command_task(args: argparse.Namespace) -> int:
    paths = [agentd.LOCAL_RUNS_DIR / f"{args.task_id}.json"]
    try:
        repositories = load_repository_registry(path=agentd.multirepo_registry_path())
    except Exception:
        repositories = []
    for repository in repositories:
        if args.repository_id and repository.repository_id != args.repository_id:
            continue
        state = repository_state_dir(repository)
        paths.append(state / "runs" / f"{args.task_id}.json")
    matches = [payload for path in paths if (payload := read_json(path)) is not None]
    if not matches:
        print_json({"task_id": args.task_id, "state": "unknown"})
        return 1
    print_json(matches[0] if len(matches) == 1 else {"matches": matches})
    return 0


def command_validate(args: argparse.Namespace) -> int:
    path = Path(args.path)
    try:
        task = agentd.load_task_file(path)
    except Exception as exc:
        print_json({"valid": False, "error": f"{type(exc).__name__}: {exc}"})
        return 1
    payload = {
        "valid": True,
        "id": task["id"],
        "task_digest": agentd.task_digest(task),
        "command_timeout": core.command_timeout_for(task),
        "idle_timeout": idle_timeout_for(task),
        "task_timeout": task_timeout_for(task),
    }
    if getattr(args, "repository", None):
        payload["preflight"] = task_preflight(task, args)
    print_json(payload)
    return 0 if payload.get("preflight", {}).get("ready", True) else 1


def task_preflight(task: dict[str, Any], args: argparse.Namespace) -> dict[str, Any]:
    """Read local target evidence without acquiring execution leases or repairing it."""
    checks = [_check("operator_enabled", not is_disabled(), "global operator disable marker")]
    report: dict[str, Any] = {"ready": False, "checks": checks}
    try:
        target = resolve_execution_target(args.repository, path=args.catalog)
        report.update({
            "repository_id": target.repository_id,
            "repository": target.repository,
            "agent_binding": target.agent_binding,
            "work_branch": task.get("work_branch", "main"),
            "resources": list(task_resources_for(task)),
            "memory_limit_mb": memory_limit_for(task),
            "resource_admission": "checked_by_executor",
        })
        registry = load_repository_registry(path=args.registry or agentd.multirepo_registry_path())
        matches = [
            item
            for item in registry
            if item.repository_id.casefold() == target.repository_id.casefold()
        ]
        if len(matches) != 1:
            raise ValueError(f"target is not enabled in the local registry: {target.repository_id}")
        repository = matches[0]
        if (repository.repository.casefold() != target.repository.casefold() or
                repository.agent_binding != target.agent_binding):
            raise ValueError("local registry identity differs from the target catalog")
        require_task_agent_binding(task, target.agent_binding)
        validate_repository_control_binding(
            repository_id=repository.repository_id,
            repository=repository.repository,
            expected_agent_binding=target.agent_binding,
            control_dir=repository.control,
            catalog_path=args.catalog,
        )
        validate_repository(repository)
        core.validate_branch(task.get("work_branch", "main"), cwd=repository.control)
        checks.append(_check("target_admission", True, "catalog, registry, control and task identities agree"))
    except Exception as exc:
        checks.append(_check("target_admission", False, f"{type(exc).__name__}: {exc}"))
    report["ready"] = all(check["ok"] for check in checks)
    if not report["ready"]:
        report["blocked_reason"] = next(check["detail"] for check in checks if not check["ok"])
    return report


def command_prepare(args: argparse.Namespace) -> int:
    try:
        with Path(args.path).open("rb") as handle:
            content = handle.read(MAX_TASK_FILE_BYTES + 1)
        if len(content) > MAX_TASK_FILE_BYTES:
            raise ValueError(f"task draft exceeds {MAX_TASK_FILE_BYTES} bytes")
        task = prepare_task(
            json.loads(content), repository=args.repository, profile=args.profile,
            catalog_path=args.catalog,
        )
        output_dir = args.output_dir.resolve()
        registry_path = args.registry or agentd.multirepo_registry_path()
        for repository in load_repository_registry(path=registry_path, include_disabled=True):
            if output_dir == repository.control or repository.control in output_dir.parents:
                raise ValueError("prepare-task cannot write into a daemon control checkout; use a publication checkout")
        manifest = write_task_bundle(output_dir, task)
        print_json({
            "prepared": True,
            "manifest": str(manifest),
            "agent_binding": task["agent_binding"],
            "task_digest": agentd.task_digest(task),
            "profile": args.profile,
            "resources": list(task_resources_for(task)),
            "command_timeout": core.command_timeout_for(task),
            "idle_timeout": idle_timeout_for(task),
            "task_timeout": task_timeout_for(task),
        })
        return 0
    except Exception as exc:
        print_json({"prepared": False, "error": f"{type(exc).__name__}: {exc}"})
        return 1


def _check(name: str, ok: bool, detail: str = "") -> dict[str, Any]:
    return {"name": name, "ok": ok, "detail": detail}


def _pid_alive(pid: Any) -> bool:
    if not isinstance(pid, int) or isinstance(pid, bool) or pid < 2:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _status_owner_pid(payload: dict[str, Any]) -> int | None:
    """Return the process that owns freshness of the current status payload."""
    for key in ("entrypoint_pid", "supervisor_pid", "pid"):
        value = payload.get(key)
        if isinstance(value, int) and not isinstance(value, bool) and value >= 2:
            return value
    return None


def _status_with_liveness(payload: dict[str, Any]) -> dict[str, Any]:
    """Annotate status freshness and never present a dead owner as live state."""
    result = dict(payload)
    owner_pid = _status_owner_pid(payload)
    alive = _pid_alive(owner_pid)
    result["status_owner_pid"] = owner_pid
    result["process_alive"] = alive
    if not alive:
        result["last_state"] = payload.get("state")
        result["state"] = "stale"
        result["error"] = "status owner process is not running"
    return result


def _bounded_tree_stats(path: Path) -> tuple[int, int, bool]:
    files = 0
    size = 0
    if not path.exists():
        return files, size, False
    for root, _directories, names in os.walk(path):
        for name in names:
            files += 1
            if files > storage.DIAGNOSTIC_FILE_LIMIT:
                return files, size, True
            try:
                size += (Path(root) / name).lstat().st_size
            except OSError:
                pass
    return files, size, False


def _control_history(repository: RepositoryContext) -> dict[str, Any]:
    count = core.process(
        ["git", "rev-list", "--count", repository.control_branch],
        repository.control,
        timeout=30,
        log_commands=False,
    )
    commits = (
        int(str(count.get("output", "")).strip())
        if count["exit_code"] == 0 and str(count.get("output", "")).strip().isdigit()
        else None
    )
    files, size, truncated = _bounded_tree_stats(repository.control / ".git")
    shallow = storage.git_bool(
        core,
        repository.control,
        ["rev-parse", "--is-shallow-repository"],
    )
    sparse = storage.git_bool(
        core,
        repository.control,
        ["config", "--bool", "core.sparseCheckout"],
    )
    partial_clone = storage.git_bool(
        core,
        repository.control,
        ["config", "--bool", "remote.origin.promisor"],
    )
    warning = bool(
        (commits is not None and commits > storage.CONTROL_HISTORY_WARNING_COMMITS)
        or size >= storage.CONTROL_HISTORY_WARNING_BYTES
        or truncated
        or shallow is not True
        or sparse is not True
        or partial_clone is not True
    )
    return {
        "repository_id": repository.repository_id,
        "target_depth": storage.CONTROL_HISTORY_DEPTH,
        "commits": commits,
        "git_bytes": size,
        "git_files": files,
        "scan_truncated": truncated,
        "shallow": shallow,
        "sparse": sparse,
        "partial_clone": partial_clone,
        "warning": warning,
    }


def _workspace_storage(repository: RepositoryContext) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for kind, path, warning_bytes in (
        ("control", repository.control, storage.CONTROL_WORKTREE_WARNING_BYTES),
        ("work", repository.work, storage.WORKTREE_WARNING_BYTES),
    ):
        files, size, truncated = _bounded_tree_stats(path)
        result.append(
            {
                "repository_id": repository.repository_id,
                "kind": kind,
                "path": str(path),
                "files": files,
                "bytes": size,
                "scan_truncated": truncated,
                "warning_threshold_bytes": warning_bytes,
                "warning": bool(size >= warning_bytes or truncated),
            }
        )
    return result


def command_doctor(_args: argparse.Namespace) -> int:
    checks: list[dict[str, Any]] = []
    warnings: list[str] = []
    checks.append(_check("self_repo", (agentd.SELF_REPO / ".git").exists(), str(agentd.SELF_REPO)))
    checks.append(_check("status_file", agentd.LOCAL_STATUS_PATH.exists(), str(agentd.LOCAL_STATUS_PATH)))

    try:
        status = read_json(agentd.LOCAL_STATUS_PATH) or {}
        pid = _status_owner_pid(status)
        checks.append(_check("daemon_process", _pid_alive(pid), f"pid={pid}"))
    except Exception as exc:
        checks.append(_check("daemon_process", False, str(exc)))

    repositories: list[RepositoryContext] = []
    try:
        repositories = load_repository_registry(path=agentd.multirepo_registry_path())
        checks.append(_check("repository_registry", True, f"repositories={len(repositories)}"))
    except Exception as exc:
        checks.append(_check("repository_registry", False, f"{type(exc).__name__}: {exc}"))

    history: list[dict[str, Any]] = []
    workspace_storage: list[dict[str, Any]] = []
    checkpoint_stats: list[dict[str, Any]] = []
    pending_claims = 0
    pending_results = 0
    for repository in repositories:
        try:
            validate_repository(repository)
        except Exception as exc:
            checks.append(
                _check(
                    f"repository:{repository.repository_id}",
                    False,
                    f"{type(exc).__name__}: {exc}",
                )
            )
            continue
        checks.append(
            _check(
                f"repository:{repository.repository_id}",
                True,
                repository.repository,
            )
        )
        state = agentd.STATE_DIR / "repositories" / repository.repository_id
        pending_claims += len(list((state / "claims").glob("*.json")))
        pending_results += len(list((state / "result-spool").glob("*.json")))
        files, size, truncated = _bounded_tree_stats(repository.checkpoints)
        checkpoint_stats.append(
            {
                "repository_id": repository.repository_id,
                "files": files,
                "bytes": size,
                "scan_truncated": truncated,
            }
        )
        control = _control_history(repository)
        history.append(control)
        if control["warning"]:
            warnings.append(
                f"control storage policy drift for {repository.repository_id}: "
                f"commits={control['commits']} bytes={control['git_bytes']} "
                f"shallow={control['shallow']} sparse={control['sparse']} "
                f"partial={control['partial_clone']}"
            )

        for workspace in _workspace_storage(repository):
            workspace_storage.append(workspace)
            if workspace["warning"]:
                warnings.append(
                    f"{workspace['kind']} workspace is large for "
                    f"{repository.repository_id}: bytes={workspace['bytes']} "
                    f"files={workspace['files']} "
                    f"scan_truncated={workspace['scan_truncated']}"
                )

    ok = all(item["ok"] for item in checks)
    print_json(
        {
            "ok": ok,
            "release_version": RELEASE_VERSION,
            "storage_policy": {
                "control_history_depth": storage.CONTROL_HISTORY_DEPTH,
                "control_sparse_paths": list(storage.CONTROL_SPARSE_PATHS),
                "automatic_destructive_cleanup": False,
            },
            "checks": checks,
            "pending_claims": pending_claims,
            "pending_result_publications": pending_results,
            "checkpoint_stats": checkpoint_stats,
            "workspace_storage": workspace_storage,
            "control_history": history,
            "warnings": warnings,
        }
    )
    return 0 if ok else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Local Agent control and diagnostics")
    sub = parser.add_subparsers(dest="command", required=True)

    backlog = sub.add_parser(
        "backlog", help="read-only local claim/result-spool census (no replay)"
    )
    backlog.add_argument("--state-root", type=Path, help="explicit local state root")
    backlog.add_argument("--registry-file", type=Path, help="explicit local registry file")
    backlog.set_defaults(func=command_backlog)
    register_host_claims_parser(sub)
    health = sub.add_parser(
        "health", help="bounded local heartbeat/registry observation (read-only)"
    )
    health.add_argument("--status-file", type=Path, help="override status path for offline inspection")
    health.add_argument("--registry-file", type=Path, help="override registry path for offline inspection")
    health.add_argument("--max-age-seconds", type=int, default=180)
    health.set_defaults(func=command_health)
    register_portable_cli_parsers(sub)
    sub.add_parser("status", help="show local daemon status").set_defaults(func=command_status)
    task = sub.add_parser("task", help="show local task progress")
    task.add_argument("task_id")
    task.add_argument("--repository-id")
    task.set_defaults(func=command_task)

    validate = sub.add_parser("validate-task", help="validate a task JSON file")
    validate.add_argument("path")
    validate.add_argument("--repository", help="explicit catalog id or owner/name for local admission preflight")
    validate.add_argument("--registry", type=Path, help="local repository registry path")
    validate.add_argument("--catalog", type=Path, help="canonical binding catalog path")
    validate.set_defaults(func=command_validate)

    prepare = sub.add_parser("prepare-task", help="compile a draft into a target-bound task bundle")
    prepare.add_argument("path")
    prepare.add_argument("--repository", required=True, help="explicit catalog id or owner/name")
    prepare.add_argument("--profile", choices=EXECUTION_PROFILES, default="repository")
    prepare.add_argument("--output-dir", type=Path, required=True, help="publication staging directory, never a daemon control clone")
    prepare.add_argument("--registry", type=Path, help="local repository registry path")
    prepare.add_argument("--catalog", type=Path, help="canonical binding catalog path")
    prepare.set_defaults(func=command_prepare)

    host_mailbox = sub.add_parser(
        "prepare-host-mailbox", help="stage a targeted host task offline (no GitHub publication or execution)"
    )
    host_mailbox.add_argument("path", help="task draft JSON file")
    host_mailbox.add_argument("--repository", required=True, help="canonical catalog repository")
    host_mailbox.add_argument("--host-id", required=True, help="explicit enrolled host")
    host_mailbox.add_argument("--expected-operator-sha", required=True, help="40-character pinned operator-control SHA")
    host_mailbox.add_argument("--profile", choices=EXECUTION_PROFILES, default="repository")
    host_mailbox.add_argument("--output-root", required=True, type=Path, help="isolated absolute publication staging root")
    host_mailbox.add_argument("--registry", type=Path, help="local registry for exclusion of active workspaces")
    host_mailbox.add_argument("--catalog", type=Path, help="canonical binding catalog")
    host_mailbox.set_defaults(func=command_prepare_host_mailbox)

    preview_host = sub.add_parser(
        "preview-host-mailbox", help="inspect one exact Git host task snapshot (never execute)"
    )
    preview_host.add_argument("task_id")
    preview_host.add_argument("--repository", required=True)
    preview_host.add_argument("--host-id", required=True)
    preview_host.add_argument("--expected-control-sha", required=True)
    preview_host.add_argument("--expected-operator-sha", required=True)
    preview_host.add_argument("--registry", type=Path)
    preview_host.add_argument("--catalog", type=Path)
    preview_host.set_defaults(func=command_preview_host_mailbox)

    preview_status = sub.add_parser(
        "preview-host-status", help="read one pinned Git host observation (no execution or publication)"
    )
    preview_status.add_argument("--repository", required=True)
    preview_status.add_argument("--host-id", required=True)
    preview_status.add_argument("--expected-control-sha", required=True)
    preview_status.add_argument("--expected-operator-sha", required=True)
    preview_status.add_argument("--expected-source-sha", required=True)
    preview_status.add_argument("--max-age-seconds", type=int, default=120)
    preview_status.add_argument("--registry", type=Path)
    preview_status.add_argument("--catalog", type=Path)
    preview_status.set_defaults(func=command_preview_host_status)

    preview_facts = sub.add_parser(
        "preview-host-facts-publication",
        help="read-only host facts Git SHA/CAS preflight (never push or enroll)",
    )
    preview_facts.add_argument("--host-id", required=True)
    preview_facts.add_argument("--identity-file", required=True, type=Path)
    preview_facts.add_argument("--staging-root", required=True, type=Path)
    preview_facts.add_argument("--repository", required=True)
    preview_facts.add_argument("--expected-operator-sha", required=True)
    preview_facts.add_argument("--expected-control-sha", required=True)
    preview_facts.add_argument("--registry", type=Path)
    preview_facts.add_argument("--catalog", type=Path)
    preview_facts.set_defaults(func=command_preview_host_facts_publication)

    init_identity = sub.add_parser(
        "init-host-identity", help="create a local host UUID once (no enrollment or GitHub publication)"
    )
    init_identity.add_argument("--host-id", required=True)
    init_identity.add_argument("--output-file", type=Path, required=True)
    init_identity.set_defaults(func=command_init_host_identity)

    stage_facts = sub.add_parser(
        "stage-host-facts", help="stage sanitized enrolled-host facts offline (no publication or heartbeat)"
    )
    stage_facts.add_argument("--host-id", required=True)
    stage_facts.add_argument("--identity-file", required=True, type=Path,
                             help="existing, locally configured host identity JSON")
    stage_facts.add_argument("--expected-operator-sha", required=True)
    stage_facts.add_argument("--source-revision", required=True)
    stage_facts.add_argument("--output-root", required=True, type=Path)
    stage_facts.add_argument("--registry", type=Path)
    stage_facts.add_argument("tools", nargs="+", help="explicit local executable names")
    stage_facts.set_defaults(func=command_stage_host_facts)

    host_inventory = sub.add_parser(
        "host-inventory", help="preview sanitized host/tool facts (no enrollment or publish)"
    )
    host_inventory.add_argument("--host-id", required=True)
    host_inventory.add_argument("--source-revision", required=True)
    host_inventory.add_argument("tools", nargs="+")
    host_inventory.set_defaults(func=command_host_inventory)

    host_registry = sub.add_parser(
        "host-registry-preview",
        help="read pinned operator-control host enrollment snapshot (no admission)",
    )
    host_registry.set_defaults(func=command_host_registry_preview)

    sub.add_parser("doctor", help="run daemon installation checks").set_defaults(func=command_doctor)
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
