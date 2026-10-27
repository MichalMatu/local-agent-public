#!/usr/bin/env python3
"""Safe administrative compaction for Git-backed agent-control history."""
from __future__ import annotations

import argparse
import json
import tempfile
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

from local_agent.foundation import storage
from local_agent.repository import admin
from local_agent.repository.context import RepositoryContext, load_repository_registry
from local_agent.repository.history_policy import CONTROL_HISTORY_COMPACT_ROOT_MESSAGE

DEFAULT_COMPACTION_THRESHOLD = storage.CONTROL_HISTORY_DEPTH


@dataclass(frozen=True)
class ControlCompactionPlan:
    branch: str
    head_sha: str
    remote_sha: str
    tree_sha: str
    visible_commits: int
    shallow: bool
    threshold: int
    eligible: bool
    reason: str

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def _require_git(result, operation: str) -> str:
    if result.returncode != 0:
        raise RuntimeError(f"{operation} failed: {result.stdout.strip()}")
    return result.stdout.strip()


def _git_output(control: Path, args: list[str], operation: str, *, timeout: int = 120) -> str:
    return _require_git(admin.run_git(args, cwd=control, timeout=timeout), operation)


def _parse_remote_branch_sha(output: str, branch: str) -> str:
    lines = [line for line in output.splitlines() if line.strip()]
    if len(lines) != 1:
        raise RuntimeError(
            f"expected exactly one remote ref for {branch!r}, got {len(lines)}"
        )
    fields = lines[0].split()
    expected_ref = f"refs/heads/{branch}"
    if len(fields) < 2 or fields[1] != expected_ref:
        raise RuntimeError(f"unexpected ls-remote output for {branch!r}: {lines[0]!r}")
    return fields[0]


def _remote_branch_sha(control: Path, branch: str) -> str:
    output = _git_output(
        control,
        ["ls-remote", "--heads", "origin", f"refs/heads/{branch}"],
        "read remote control branch",
    )
    return _parse_remote_branch_sha(output, branch)


def _fetch_remote_tip(control: Path, branch: str) -> str:
    remote_ref = f"refs/remotes/origin/{branch}"
    fetch = admin.run_git(
        [
            "fetch",
            "--depth",
            str(storage.CONTROL_HISTORY_DEPTH),
            "--no-tags",
            "origin",
            f"+refs/heads/{branch}:{remote_ref}",
        ],
        cwd=control,
        timeout=300,
    )
    _require_git(fetch, "fetch remote control branch for reconciliation")
    return _git_output(control, ["rev-parse", remote_ref], "read reconciled remote tip", timeout=30)


def _is_ancestor(control: Path, ancestor: str, descendant: str) -> bool:
    result = admin.run_git(
        ["merge-base", "--is-ancestor", ancestor, descendant],
        cwd=control,
        timeout=30,
    )
    if result.returncode == 0:
        return True
    if result.returncode == 1:
        return False
    raise RuntimeError(f"inspect control ancestry failed: {result.stdout.strip()}")


def _realign_control_checkout(
    control: Path,
    branch: str,
    target_sha: str,
    *,
    expected_tree: str | None = None,
) -> None:
    storage.require_control_rebase_clear(control)
    reset = admin.run_git(["reset", "--hard", target_sha], cwd=control, timeout=120)
    _require_git(reset, "reset compacted control checkout")
    update_ref = admin.run_git(
        ["update-ref", f"refs/remotes/origin/{branch}", target_sha],
        cwd=control,
        timeout=30,
    )
    _require_git(update_ref, "update compacted remote-tracking ref")
    final_head = _git_output(control, ["rev-parse", "HEAD"], "verify compacted HEAD", timeout=30)
    if final_head != target_sha:
        raise RuntimeError("local control checkout did not converge on reconciled remote tip")
    if expected_tree is not None:
        final_tree = _git_output(
            control,
            ["rev-parse", "HEAD^{tree}"],
            "verify compacted tree",
            timeout=30,
        )
        if final_tree != expected_tree:
            raise RuntimeError("control tree changed during compaction")


def plan_control_compaction(
    control: Path,
    branch: str,
    *,
    threshold: int = DEFAULT_COMPACTION_THRESHOLD,
) -> ControlCompactionPlan:
    """Inspect a control checkout without rewriting history."""
    if threshold < 1:
        raise ValueError("compaction threshold must be positive")
    storage.require_control_rebase_clear(control)

    dirty = _git_output(
        control,
        ["status", "--porcelain=v1", "--untracked-files=all"],
        "inspect control checkout",
        timeout=30,
    )
    current_branch = _git_output(
        control,
        ["symbolic-ref", "--quiet", "--short", "HEAD"],
        "read control branch",
        timeout=30,
    )
    head_sha = _git_output(control, ["rev-parse", "HEAD"], "read control HEAD", timeout=30)
    tree_sha = _git_output(
        control,
        ["rev-parse", "HEAD^{tree}"],
        "read control tree",
        timeout=30,
    )
    remote_sha = _remote_branch_sha(control, branch)
    visible_commits_raw = _git_output(
        control,
        ["rev-list", "--count", "HEAD"],
        "count visible control commits",
        timeout=30,
    )
    shallow_raw = _git_output(
        control,
        ["rev-parse", "--is-shallow-repository"],
        "inspect shallow control state",
        timeout=30,
    )
    try:
        visible_commits = int(visible_commits_raw)
    except ValueError as exc:
        raise RuntimeError(
            f"invalid control commit count: {visible_commits_raw!r}"
        ) from exc
    shallow = shallow_raw.lower() == "true"

    reason = "ready"
    eligible = True
    if dirty:
        eligible = False
        reason = "control checkout is dirty"
    elif current_branch != branch:
        eligible = False
        reason = f"control checkout is on {current_branch!r}, expected {branch!r}"
    elif head_sha != remote_sha:
        eligible = False
        reason = "local control HEAD does not match remote branch"
    elif visible_commits < threshold:
        eligible = False
        reason = "visible history is below compaction threshold"

    return ControlCompactionPlan(
        branch=branch,
        head_sha=head_sha,
        remote_sha=remote_sha,
        tree_sha=tree_sha,
        visible_commits=visible_commits,
        shallow=shallow,
        threshold=threshold,
        eligible=eligible,
        reason=reason,
    )


def compact_control_history(
    control: Path,
    branch: str,
    *,
    threshold: int = DEFAULT_COMPACTION_THRESHOLD,
    expected_head: str | None = None,
) -> dict[str, object]:
    """Replace eligible control history with one root commit preserving the exact tree."""
    plan = plan_control_compaction(control, branch, threshold=threshold)
    if not plan.eligible:
        return {"changed": False, "plan": plan.to_dict()}
    if expected_head is not None and plan.head_sha != expected_head:
        raise RuntimeError(
            "control branch changed after planning; refusing history compaction"
        )

    admin._require_commit_identity(control)
    new_sha = _git_output(
        control,
        ["commit-tree", plan.tree_sha, "-m", CONTROL_HISTORY_COMPACT_ROOT_MESSAGE],
        "create compacted control root commit",
        timeout=60,
    )
    new_tree = _git_output(
        control,
        ["rev-parse", f"{new_sha}^{{tree}}"],
        "verify compacted control tree",
        timeout=30,
    )
    if new_tree != plan.tree_sha:
        raise RuntimeError(
            "compacted control tree differs from source tree; refusing remote update"
        )

    # Protect the explicit history rewrite if another Git process started a
    # rebase after the read-only planning phase.
    storage.require_control_rebase_clear(control)
    push = admin.run_git(
        [
            "push",
            f"--force-with-lease=refs/heads/{branch}:{plan.remote_sha}",
            "origin",
            f"{new_sha}:refs/heads/{branch}",
        ],
        cwd=control,
        timeout=300,
    )
    push_succeeded = push.returncode == 0
    reconciled_tip = new_sha
    remote_advanced = False

    if push_succeeded:
        _realign_control_checkout(
            control,
            branch,
            new_sha,
            expected_tree=plan.tree_sha,
        )
        observed_remote = _remote_branch_sha(control, branch)
        if observed_remote != new_sha:
            fetched_remote = _fetch_remote_tip(control, branch)
            if not _is_ancestor(control, new_sha, fetched_remote):
                raise RuntimeError(
                    "remote control branch moved to a non-descendant after compaction"
                )
            reconciled_tip = fetched_remote
            remote_advanced = fetched_remote != new_sha
            _realign_control_checkout(control, branch, reconciled_tip)
    else:
        observed_remote = _remote_branch_sha(control, branch)
        if observed_remote == new_sha:
            _realign_control_checkout(
                control,
                branch,
                new_sha,
                expected_tree=plan.tree_sha,
            )
        else:
            fetched_remote = _fetch_remote_tip(control, branch)
            if not _is_ancestor(control, new_sha, fetched_remote):
                raise RuntimeError(
                    "publish compacted control history failed: "
                    f"{push.stdout.strip()}; remote now {fetched_remote}"
                )
            reconciled_tip = fetched_remote
            remote_advanced = fetched_remote != new_sha
            _realign_control_checkout(control, branch, reconciled_tip)

    if reconciled_tip != new_sha and not _is_ancestor(control, new_sha, reconciled_tip):
        raise RuntimeError("reconciled control tip does not descend from compacted root")

    return {
        "changed": True,
        "old_head": plan.head_sha,
        "new_head": new_sha,
        "head_sha": reconciled_tip,
        "tree_sha": plan.tree_sha,
        "visible_commits_before": plan.visible_commits,
        "push_reconciled": not push_succeeded,
        "remote_advanced": remote_advanced,
    }


def backup_control_history(
    repository: RepositoryContext,
    destination: Path,
    *,
    expected_head: str,
) -> Path:
    """Create and verify a full bundle of the remote control branch before rewriting it."""
    destination.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    safe_id = repository.repository_id.replace("/", "-")
    bundle_path = destination / f"{safe_id}-agent-control-{timestamp}-{expected_head[:12]}.bundle"
    if bundle_path.exists():
        raise RuntimeError(f"backup bundle already exists: {bundle_path}")

    with tempfile.TemporaryDirectory(prefix=f"local-agent-{safe_id}-backup-") as tmp:
        mirror = Path(tmp) / "control.git"
        clone = admin.run_git(
            [
                "clone",
                "--bare",
                "--single-branch",
                "--branch",
                repository.control_branch,
                admin.clone_url(repository),
                str(mirror),
            ],
            timeout=3600,
        )
        _require_git(clone, "clone full control branch history for backup")

        backup_head = _git_output(
            mirror,
            ["rev-parse", f"refs/heads/{repository.control_branch}"],
            "read backup control branch",
            timeout=30,
        )
        if backup_head != expected_head:
            raise RuntimeError(
                "remote control branch changed while preparing backup; refusing compaction"
            )

        bundle = admin.run_git(
            [
                "bundle",
                "create",
                str(bundle_path),
                f"refs/heads/{repository.control_branch}",
            ],
            cwd=mirror,
            timeout=3600,
        )
        _require_git(bundle, "create control history backup bundle")
        verify = admin.run_git(
            ["bundle", "verify", str(bundle_path)],
            cwd=mirror,
            timeout=3600,
        )
        try:
            _require_git(verify, "verify control history backup bundle")
        except Exception:
            bundle_path.unlink(missing_ok=True)
            raise

    return bundle_path


def _selected_repositories(args: argparse.Namespace) -> list[RepositoryContext]:
    repositories = load_repository_registry(path=args.registry)
    if args.all:
        return list(repositories)
    return admin.select_repositories(repositories, args.repository_id)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Inspect or compact local-agent agent-control Git history safely."
    )
    parser.add_argument("--registry", type=Path)
    selector = parser.add_mutually_exclusive_group(required=True)
    selector.add_argument("--repository-id")
    selector.add_argument("--all", action="store_true")
    parser.add_argument(
        "--threshold",
        type=int,
        default=DEFAULT_COMPACTION_THRESHOLD,
        help="minimum locally visible commit count required for compaction",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="rewrite eligible remote control branches; default is dry-run",
    )
    parser.add_argument(
        "--backup-dir",
        type=Path,
        help="required with --apply; stores verified pre-rewrite Git bundles",
    )
    args = parser.parse_args()
    if args.threshold < 1:
        parser.error("--threshold must be positive")
    if args.apply and args.backup_dir is None:
        parser.error("--backup-dir is required with --apply")
    return args


def main() -> int:
    args = parse_args()
    selected = _selected_repositories(args)
    lock_handle = None
    if args.apply:
        from local_agent.daemon import service as agentd

        lock_handle = agentd.acquire_daemon_lock()

    try:
        for repository in selected:
            admin.validate_checkout(repository.control, repository, "control")
            plan = plan_control_compaction(
                repository.control,
                repository.control_branch,
                threshold=args.threshold,
            )
            record: dict[str, object] = {
                "repository_id": repository.repository_id,
                "repository": repository.repository,
                "mode": "apply" if args.apply else "dry-run",
                "plan": plan.to_dict(),
            }
            if args.apply and plan.eligible:
                assert args.backup_dir is not None
                backup = backup_control_history(
                    repository,
                    args.backup_dir,
                    expected_head=plan.head_sha,
                )
                result = compact_control_history(
                    repository.control,
                    repository.control_branch,
                    threshold=args.threshold,
                    expected_head=plan.head_sha,
                )
                record["backup_bundle"] = str(backup)
                record["result"] = result
            print(json.dumps(record, sort_keys=True))
    finally:
        if lock_handle is not None:
            lock_handle.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
