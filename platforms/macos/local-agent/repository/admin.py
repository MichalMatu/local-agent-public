#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path
from typing import Iterable

import local_agent.foundation.core as core
from local_agent.foundation import storage
from local_agent.repository.context import RepositoryContext, load_repository_registry
from local_agent.repository.checkout_identity import (normalize_remote_url as normalize_remote_url,
                                                      require_checkout_origin)
from local_agent.repository.history_policy import CONTROL_HISTORY_INITIAL_ROOT_MESSAGE

_AGENT_CONTROL_DIRS = (
    ".agent/tasks",
    ".agent/results",
    ".agent/runs",
    ".agent/status",
    ".agent/daemon/acks",
)


def clone_url(repository: RepositoryContext) -> str:
    return f"https://github.com/{repository.repository}.git"


def run_git(
    args: list[str],
    *,
    cwd: Path | None = None,
    timeout: int = 300,
) -> subprocess.CompletedProcess[str]:
    result = core.process(
        ["git", *args],
        cwd or Path.cwd(),
        timeout=timeout,
        log_commands=False,
    )
    return subprocess.CompletedProcess(
        args=["git", *args],
        returncode=int(result["exit_code"]),
        stdout=str(result.get("output", "")),
        stderr=None,
    )


def _require_git_success(result: subprocess.CompletedProcess[str], operation: str) -> None:
    if result.returncode != 0:
        raise RuntimeError(f"{operation} failed: {result.stdout.strip()}")


def validate_checkout(path: Path, repository: RepositoryContext, label: str) -> None:
    if not (path / ".git").exists():
        raise RuntimeError(f"{label} checkout missing: {path}")
    remote = run_git(["remote", "get-url", "origin"], cwd=path, timeout=30)
    require_checkout_origin(
        path, repository, label, returncode=remote.returncode, output=remote.stdout,
    )


def validate_repository(repository: RepositoryContext) -> None:
    validate_checkout(repository.control, repository, "control")
    validate_checkout(repository.work, repository, "work")
    if repository.control.resolve() == repository.work.resolve():
        raise RuntimeError("control and work checkouts must be different paths")


def remote_branch_exists(repository: RepositoryContext, branch: str) -> bool:
    result = run_git(
        [
            "ls-remote",
            "--exit-code",
            "--heads",
            clone_url(repository),
            f"refs/heads/{branch}",
        ],
        timeout=120,
    )
    if result.returncode == 0:
        return True
    if result.returncode == 2:
        return False
    raise RuntimeError(f"remote branch probe failed: {result.stdout.strip()}")


def _clone_if_missing(
    repository: RepositoryContext,
    *,
    path: Path,
    branch: str,
    single_branch: bool,
    label: str,
    shallow_depth: int | None = None,
    partial_clone: bool = False,
    sparse_paths: tuple[str, ...] = (),
    no_tags: bool = False,
) -> bool:
    if path.exists():
        validate_checkout(path, repository, label)
        return False

    path.parent.mkdir(parents=True, exist_ok=True)
    command = ["clone", "--branch", branch]
    if single_branch:
        command.append("--single-branch")
    if shallow_depth is not None:
        if shallow_depth < 1:
            raise ValueError("shallow_depth must be positive")
        command.extend(["--depth", str(shallow_depth)])
    if partial_clone:
        command.append("--filter=blob:none")
    if sparse_paths:
        command.append("--sparse")
    if no_tags:
        command.append("--no-tags")
    command.extend([clone_url(repository), str(path)])
    result = run_git(command, timeout=900)
    if result.returncode != 0:
        raise RuntimeError(f"{label} clone failed: {result.stdout.strip()}")
    validate_checkout(path, repository, label)

    if sparse_paths:
        sparse = run_git(
            ["sparse-checkout", "set", *sparse_paths],
            cwd=path,
            timeout=120,
        )
        _require_git_success(sparse, f"configure {label} sparse checkout")
    return True


def _require_commit_identity(path: Path) -> None:
    for key in ("user.name", "user.email"):
        result = run_git(["config", "--get", key], cwd=path, timeout=30)
        if result.returncode != 0 or not result.stdout.strip():
            raise RuntimeError(
                f"Git {key} is required to initialize an agent-control branch in {path}"
            )


def initialize_control_branch(repository: RepositoryContext) -> None:
    """Create a new agent-control branch only in a freshly provisioned control clone."""
    _require_commit_identity(repository.control)
    switch = run_git(
        ["switch", "--orphan", repository.control_branch],
        cwd=repository.control,
        timeout=120,
    )
    _require_git_success(switch, "create control branch")

    clean = run_git(["clean", "-fdx"], cwd=repository.control, timeout=120)
    _require_git_success(clean, "clean fresh control checkout")
    for directory in _AGENT_CONTROL_DIRS:
        target = repository.control / directory
        target.mkdir(parents=True, exist_ok=True)
        (target / ".gitkeep").write_text("", encoding="utf-8")

    add = run_git(["add", ".agent"], cwd=repository.control, timeout=120)
    _require_git_success(add, "stage control branch skeleton")
    commit = run_git(
        ["commit", "-m", CONTROL_HISTORY_INITIAL_ROOT_MESSAGE],
        cwd=repository.control,
        timeout=120,
    )
    _require_git_success(commit, "commit control branch skeleton")
    push = run_git(
        ["push", "-u", "origin", repository.control_branch],
        cwd=repository.control,
        timeout=300,
    )
    _require_git_success(push, "publish control branch skeleton")


def provision_repository(repository: RepositoryContext) -> dict[str, bool]:
    """Explicitly create missing control/work clones without overwriting existing paths."""
    control_branch_created = False
    if repository.control.exists():
        validate_checkout(repository.control, repository, "control")
        control_created = False
    elif remote_branch_exists(repository, repository.control_branch):
        control_created = _clone_if_missing(
            repository,
            path=repository.control,
            branch=repository.control_branch,
            single_branch=True,
            label="control",
            shallow_depth=storage.CONTROL_HISTORY_DEPTH,
            partial_clone=True,
            sparse_paths=storage.CONTROL_SPARSE_PATHS,
            no_tags=True,
        )
    else:
        control_created = _clone_if_missing(
            repository,
            path=repository.control,
            branch=repository.default_branch,
            single_branch=True,
            label="control",
            shallow_depth=1,
            partial_clone=True,
            sparse_paths=storage.CONTROL_SPARSE_PATHS,
            no_tags=True,
        )
        initialize_control_branch(repository)
        control_branch_created = True

    work_created = _clone_if_missing(
        repository,
        path=repository.work,
        branch=repository.default_branch,
        single_branch=False,
        label="work",
    )
    repository.checkpoints.mkdir(parents=True, exist_ok=True)
    validate_repository(repository)
    return {
        "control_created": control_created,
        "control_branch_created": control_branch_created,
        "work_created": work_created,
    }


def select_repositories(
    repositories: Iterable[RepositoryContext],
    repository_id: str | None,
) -> list[RepositoryContext]:
    selected = list(repositories)
    if repository_id is None:
        return selected
    matches = [item for item in selected if item.repository_id == repository_id]
    if not matches:
        raise ValueError(f"repository id is not enabled: {repository_id!r}")
    return matches


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Administer local-agent repository workspaces.")
    parser.add_argument("--registry", type=Path)
    subparsers = parser.add_subparsers(dest="command", required=True)

    list_parser = subparsers.add_parser("list", help="List enabled repositories.")
    list_parser.add_argument("--repository-id")

    validate_parser = subparsers.add_parser(
        "validate", help="Validate repository workspace checkout identity."
    )
    validate_parser.add_argument("--repository-id")

    provision_parser = subparsers.add_parser(
        "provision", help="Explicitly create missing repository workspaces."
    )
    provision_parser.add_argument("--repository-id", required=True)

    planned = subparsers.add_parser(
        "plan-add", help="Preview a project registration without any changes or Git traffic."
    )
    planned.add_argument("repository", help="canonical GitHub owner/name")
    planned.add_argument("--repository-id", help="local unique repository label")
    planned.add_argument(
        "--work-dir", type=Path,
        help="reuse an existing local checkout, or choose a future clone destination",
    )
    planned.add_argument("--control-dir", type=Path, help="independent agent-control checkout")
    planned.add_argument("--checkpoints-dir", type=Path)
    planned.add_argument("--catalog", type=Path, help="local canonical binding catalog")

    status_parser = subparsers.add_parser(
        "status", help="Read registered projects including disabled ones (no runtime probe)."
    )
    status_parser.add_argument("--repository-id")

    snapshots = subparsers.add_parser(
        "snapshots", help="Read retained isolated attached-task snapshots (no promotion)."
    )
    snapshots.add_argument("--repository-id", required=True)
    snapshots.add_argument("--limit", type=int, default=25)
    snapshots.add_argument("--snapshot-id", help="exact retained task directory ID")
    snapshots.add_argument(
        "--compare-snapshot-id",
        help="compare two completed private results; requires --snapshot-id (no patch output)",
    )

    disabled = subparsers.add_parser(
        "disable", help="Plan or apply a guarded offline registry disable."
    )
    disabled.add_argument("--repository-id", required=True)
    disabled.add_argument("--expected-sha256", help="exact digest from disable plan")
    disabled.add_argument("--apply", action="store_true")

    enabled = subparsers.add_parser(
        "enable", help="Plan or apply a guarded offline registry re-enable."
    )
    enabled.add_argument("--repository-id", required=True)
    enabled.add_argument("--expected-sha256", help="exact digest from enable plan")
    enabled.add_argument("--apply", action="store_true")

    detached = subparsers.add_parser(
        "detach", help="Plan or apply a guarded registry-only detach."
    )
    detached.add_argument("--repository-id", required=True)
    detached.add_argument("--expected-sha256", help="exact digest from detach plan")
    detached.add_argument("--apply", action="store_true")

    added = subparsers.add_parser("add", help="Register a project (requires --apply).")
    added.add_argument("repository", help="canonical GitHub owner/name")
    added.add_argument("--repository-id")
    added.add_argument("--work-dir", type=Path)
    added.add_argument("--control-dir", type=Path)
    added.add_argument("--checkpoints-dir", type=Path)
    added.add_argument("--catalog", type=Path)
    added.add_argument("--apply", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.command == "snapshots":
        from local_agent.repository.registration_status import repository_registration_status
        from local_agent.repository.snapshot_review import inspect_attached_snapshots
        from local_agent.repository.snapshot_compare import compare_attached_snapshots

        try:
            config = repository_registration_status(
                registry_path=args.registry, repository_id=args.repository_id,
            )
            entry = config["repositories"][0]
            if entry["work_mode"] != "attached":
                raise ValueError("snapshot review requires an attached repository")
            # Keep the declared path, not the canonical collision identity.
            checkpoints = Path(entry["checkpoints_declared_dir"])
            if args.compare_snapshot_id is not None:
                if args.snapshot_id is None:
                    raise ValueError("--compare-snapshot-id requires --snapshot-id")
                snapshot = compare_attached_snapshots(
                    checkpoints, args.snapshot_id, args.compare_snapshot_id,
                )
            else:
                snapshot = inspect_attached_snapshots(
                    checkpoints, limit=args.limit, snapshot_id=args.snapshot_id,
                )
            result = {
                **snapshot,
                "repository_id": entry["repository_id"],
                "repository_enabled": entry["enabled"],
                "registry_only": True,
                "execution_authority": "not_asserted",
                "changes_applied": False,
            }
        except (OSError, ValueError, RuntimeError, UnicodeError) as exc:
            print(json.dumps({
                "status": "unverified", "changes_applied": False,
                "error": f"{type(exc).__name__}: {exc}",
            }, sort_keys=True))
            return 2
        print(json.dumps(result, sort_keys=True, indent=2))
        return 0
    if args.command == "detach":
        from local_agent.repository.registration_detach import (
            apply_repository_detach, plan_repository_detach,
        )

        try:
            if args.apply:
                result = apply_repository_detach(
                    args.repository_id, expected_sha256=args.expected_sha256,
                    registry_path=args.registry,
                )
            else:
                if args.expected_sha256 is not None:
                    raise ValueError("--expected-sha256 is only accepted with --apply")
                result = plan_repository_detach(
                    args.repository_id, registry_path=args.registry,
                )
        except (OSError, ValueError, RuntimeError, UnicodeError) as exc:
            print(json.dumps({
                "status": "unverified", "changes_applied": False,
                "error": f"{type(exc).__name__}: {exc}",
            }, sort_keys=True))
            return 2
        print(json.dumps(result, sort_keys=True, indent=2))
        return 0
    if args.command == "enable":
        from local_agent.repository.registration_enable import (
            apply_repository_enable, plan_repository_enable,
        )
        try:
            if args.apply:
                result = apply_repository_enable(
                    args.repository_id, expected_sha256=args.expected_sha256,
                    registry_path=args.registry,
                )
            else:
                if args.expected_sha256 is not None:
                    raise ValueError("--expected-sha256 is only accepted with --apply")
                result = plan_repository_enable(
                    args.repository_id, registry_path=args.registry,
                )
        except (OSError, ValueError, RuntimeError, UnicodeError) as exc:
            print(json.dumps({
                "status": "unverified", "changes_applied": False,
                "error": f"{type(exc).__name__}: {exc}",
            }, sort_keys=True))
            return 2
        print(json.dumps(result, sort_keys=True, indent=2))
        return 0
    if args.command == "disable":
        from local_agent.repository.registration_disable import (
            apply_repository_disable, plan_repository_disable,
        )

        try:
            if args.apply:
                result = apply_repository_disable(
                    args.repository_id, expected_sha256=args.expected_sha256,
                    registry_path=args.registry,
                )
            else:
                if args.expected_sha256 is not None:
                    raise ValueError("--expected-sha256 is only accepted with --apply")
                result = plan_repository_disable(
                    args.repository_id, registry_path=args.registry,
                )
        except (OSError, ValueError, RuntimeError, UnicodeError) as exc:
            print(json.dumps({
                "status": "unverified", "changes_applied": False,
                "error": f"{type(exc).__name__}: {exc}",
            }, sort_keys=True))
            return 2
        print(json.dumps(result, sort_keys=True, indent=2))
        return 0
    if args.command in ("plan-add", "add"):
        from local_agent.repository.onboarding import plan_repository_add

        try:
            action = plan_repository_add
            if args.command == "add" and args.apply:
                from local_agent.repository.transactional_add import apply_repository_add

                action = apply_repository_add
            plan = action(
                args.repository, repository_id=args.repository_id,
                work_dir=args.work_dir, control_dir=args.control_dir,
                checkpoints_dir=args.checkpoints_dir, registry_path=args.registry,
                catalog_path=args.catalog,
            )
        except (OSError, ValueError, RuntimeError, UnicodeError) as exc:
            print(json.dumps({"status": "unverified", "changes_applied": False,
                              "error": f"{type(exc).__name__}: {exc}"}, sort_keys=True))
            return 2
        print(json.dumps(plan, sort_keys=True, indent=2))
        return 0
    if args.command == "status":
        from local_agent.repository.registration_status import repository_registration_status

        try:
            snapshot = repository_registration_status(
                registry_path=args.registry, repository_id=args.repository_id,
            )
        except (OSError, ValueError, RuntimeError, UnicodeError) as exc:
            print(json.dumps({
                "status": "unverified", "changes_applied": False,
                "error": f"{type(exc).__name__}: {exc}",
            }, sort_keys=True))
            return 2
        print(json.dumps(snapshot, sort_keys=True, indent=2))
        return 0

    repositories = load_repository_registry(path=args.registry)
    selected = select_repositories(repositories, getattr(args, "repository_id", None))

    if args.command == "list":
        for repository in selected:
            print(
                f"{repository.repository_id}\t{repository.repository}\t"
                f"control={repository.control}\twork={repository.work}"
            )
        return 0

    if args.command == "validate":
        failures = 0
        for repository in selected:
            try:
                validate_repository(repository)
            except Exception as exc:
                failures += 1
                print(f"FAIL {repository.repository_id}: {type(exc).__name__}: {exc}")
            else:
                print(f"OK {repository.repository_id}: {repository.repository}")
        return 1 if failures else 0

    if args.command == "provision":
        repository = selected[0]
        result = provision_repository(repository)
        print(
            f"OK {repository.repository_id}: "
            f"control_created={result['control_created']} "
            f"control_branch_created={result['control_branch_created']} "
            f"work_created={result['work_created']}"
        )
        return 0

    raise AssertionError(f"unhandled command: {args.command}")


if __name__ == "__main__":
    raise SystemExit(main())
