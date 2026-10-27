"""Explicit, crash-resumable local repository registration.

One registry lock, immutable intent and staged clones; the user's selected
existing Git checkout is only inspected, never checked out, reset or cleaned.
No new binding/host authority is inferred: a canonical enabled catalog entry
and a matching control-mailbox binding are mandatory.
"""
from __future__ import annotations

import fcntl
import json
import os
import secrets
import shutil
import stat
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

import local_agent.foundation.core as core
from local_agent.repository.binding import (
    CONTROL_BINDING_RELATIVE, catalog_record_for_repository,
    validate_repository_control_binding,
)
from local_agent.repository.checkout_identity import validate_checkout_readonly
from local_agent.repository.context import (
    REGISTRY_VERSION, RepositoryContext, default_registry_path,
    load_repository_registry, repository_from_dict, validate_repository_set,
)
from local_agent.repository.history_policy import CONTROL_HISTORY_INITIAL_ROOT_MESSAGE
from local_agent.repository.onboarding import plan_repository_add
from local_agent.repository.snapshot_review import _checkpoint_directory
from local_agent.repository.state_layout import prepare_repository_state


def _git(args: list[str], *, cwd: Path | None = None, timeout: int = 300) -> str:
    result = core.process(
        ["git", *args], cwd or Path.cwd(), timeout=timeout, log_commands=False,
    )
    if result["exit_code"]:
        raise RuntimeError(f"repository add Git operation failed: {args[0]}: {result['output']}")
    return str(result["output"]).strip()


def _remote_url(repository: RepositoryContext) -> str:
    return f"https://github.com/{repository.repository}.git"


def _control_exists(repository: RepositoryContext) -> bool:
    result = core.process(
        ["git", "ls-remote", "--exit-code", "--heads", _remote_url(repository),
         f"refs/heads/{repository.control_branch}"],
        Path.cwd(), timeout=120, log_commands=False,
    )
    if result["exit_code"] == 0:
        return True
    if result["exit_code"] == 2:
        return False
    raise RuntimeError("control branch existence could not be verified")


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    """Atomically replace service JSON inside a pinned, no-follow parent FD.

    The pending journal directory may not exist yet. Create only that
    dedicated leaf relative to its already-pinned ancestor; never trust a
    symlinked or swapped parent pathname after a check.
    """
    if path.parent.name == "pending-repository-add":
        with _checkpoint_directory(path.parent.parent) as ancestor:
            try:
                os.mkdir(path.parent.name, mode=0o700, dir_fd=ancestor)
            except FileExistsError:
                pass
    with _checkpoint_directory(path.parent) as parent:
        try:
            current = os.stat(path.name, dir_fd=parent, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            if not stat.S_ISREG(current.st_mode):
                raise ValueError("refusing nonregular or linked JSON target")
        temporary = ".local-agent-write-" + secrets.token_hex(16)
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
        fd = os.open(temporary, flags, 0o600, dir_fd=parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, sort_keys=True, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(
                temporary, path.name, src_dir_fd=parent, dst_dir_fd=parent,
            )
            os.fsync(parent)
        finally:
            try:
                os.unlink(temporary, dir_fd=parent)
            except FileNotFoundError:
                pass


def _read_pending_journal(path: Path) -> dict[str, Any] | None:
    """Read bounded journal using pinned ancestors and a no-follow leaf."""
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
    try:
        with _checkpoint_directory(path.parent) as parent:
            try:
                fd = os.open(path.name, flags, dir_fd=parent)
            except FileNotFoundError:
                return None
            try:
                info = os.fstat(fd)
                if not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= 16384:
                    raise ValueError("untrusted repository-add journal")
                with os.fdopen(fd, "rb", closefd=False) as handle:
                    data = handle.read(16385)
                if len(data) > 16384:
                    raise ValueError("oversized repository-add journal")
                payload = json.loads(data.decode("utf-8"))
                if not isinstance(payload, dict):
                    raise ValueError("invalid repository-add journal")
                return payload
            finally:
                os.close(fd)
    except FileNotFoundError:
        return None


def _require_matching_journal(
    journal: dict[str, Any], expected: dict[str, Any], work_mode: str,
) -> None:
    if any(journal.get(key) != value for key, value in expected.items()):
        raise ValueError("pending repo-add journal identity mismatch")
    if journal.get("work_mode") != work_mode:
        raise ValueError("pending repo-add journal work mode mismatch")
    if journal.get("state") not in ("prepared", "committed"):
        raise ValueError("invalid pending repo-add journal state")


@contextmanager
def _registry_lock(path: Path) -> Iterator[None]:
    if path.is_symlink() or not path.is_file():
        raise ValueError("repo add --apply requires an existing regular registry")
    lock_path = path.with_name(path.name + ".lock")
    flags = os.O_CREAT | os.O_RDWR
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(lock_path, flags, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("another repository registration is active") from exc
        yield
    finally:
        os.close(fd)


def _ensure_checkout(path: Path, repository: RepositoryContext, label: str) -> None:
    validate_checkout_readonly(path, repository, label)
    if label == "control":
        current = _git(["branch", "--show-current"], cwd=path)
        if current != repository.control_branch:
            raise ValueError("control checkout is on the wrong branch")


def _init_bound_control(repository: RepositoryContext, path: Path) -> None:
    """Initialize only a fresh private staging clone, never a user checkout."""
    _git(["switch", "--orphan", repository.control_branch], cwd=path)
    _git(["clean", "-fdx"], cwd=path)
    for item in (
        ".agent/tasks", ".agent/results", ".agent/runs",
        ".agent/status", ".agent/daemon/acks",
    ):
        directory = path / item
        directory.mkdir(parents=True, exist_ok=True)
        (directory / ".gitkeep").write_text("", encoding="utf-8")
    identity = path / CONTROL_BINDING_RELATIVE
    identity.parent.mkdir(parents=True, exist_ok=True)
    identity.write_text(json.dumps({
        "version": 1, "repository_id": repository.repository_id,
        "repository": repository.repository, "agent_binding": repository.agent_binding,
    }, sort_keys=True) + "\n", encoding="utf-8")
    _git(["add", ".agent"], cwd=path)
    _git(["commit", "-m", CONTROL_HISTORY_INITIAL_ROOT_MESSAGE], cwd=path)
    _git(["push", "-u", "origin", repository.control_branch], cwd=path)


def _clone_to_destination(
    repository: RepositoryContext, path: Path, label: str,
    *, catalog_path: Path | None,
) -> None:
    if path.exists() or path.is_symlink():
        raise ValueError(f"{label} destination appeared during registration")
    path.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".local-agent-stage-", dir=path.parent))
    os.chmod(staging, 0o700)
    clone = staging / "checkout"
    try:
        branch_present = True if label == "work" else _control_exists(repository)
        branch = repository.default_branch if label == "work" or not branch_present else repository.control_branch
        _git(
            ["clone", "--no-tags", "--branch", branch, _remote_url(repository), str(clone)],
            timeout=900,
        )
        if label == "control" and not branch_present:
            _init_bound_control(repository, clone)
        _ensure_checkout(clone, repository, label)
        if label == "control":
            validate_repository_control_binding(
                repository_id=repository.repository_id,
                repository=repository.repository,
                expected_agent_binding=repository.agent_binding or "",
                control_dir=clone, catalog_path=catalog_path,
            )
        if path.exists() or path.is_symlink():
            raise ValueError(f"{label} destination appeared during clone")
        os.rename(clone, path)
    finally:
        # This function alone owns the uniquely named staging directory.
        # Remote control publication is durable and is reconciled on retry.
        shutil.rmtree(staging)


def _as_registry_entry(repository: RepositoryContext) -> dict[str, Any]:
    return {
        "id": repository.repository_id, "repository": repository.repository,
        "work_dir": str(repository.work), "control_dir": str(repository.control),
        "checkpoints_dir": str(repository.checkpoints),
        "work_mode": repository.work_mode,
        "agent_binding": repository.agent_binding,
        "enabled": True,
    }


def apply_repository_add(
    repository: str, *, repository_id: str | None = None,
    work_dir: str | Path | None = None, control_dir: str | Path | None = None,
    checkpoints_dir: str | Path | None = None,
    registry_path: Path | None = None, catalog_path: Path | None = None,
    home: Path | None = None, cwd: Path | None = None,
    state_dir: Path | None = None,
) -> dict[str, Any]:
    """Persist one verified binding LAST; safe retry reconciles earlier clones."""
    resolved_home = (home or Path.home()).expanduser().resolve()
    target = registry_path or default_registry_path(resolved_home)
    installation_state = state_dir or default_registry_path(resolved_home).parent
    if not target.exists():
        raise ValueError("create an explicit version-1 registry before repo add --apply")
    with _registry_lock(target):
        baseline = target.read_bytes()
        payload = json.loads(baseline)
        if not isinstance(payload, dict) or type(payload.get("version")) is not int:
            raise ValueError("invalid registry")
        if payload["version"] != REGISTRY_VERSION or not isinstance(payload.get("repositories"), list):
            raise ValueError("invalid registry version or repository list")
        plan_id = repository_id or repository.rsplit("/", 1)[-1].lower()
        prior = load_repository_registry(
            home=resolved_home, path=target, include_disabled=True, allow_empty=True,
        )
        matched = [entry for entry in prior if (
            entry.repository_id.casefold() == plan_id.casefold()
            or entry.repository.casefold() == repository.casefold()
        )]
        cat_record = catalog_record_for_repository(
            plan_id, repository, path=catalog_path,
        )
        if not cat_record.execution_enabled:
            raise ValueError("canonical execution binding is disabled")
        journal_path = target.parent / "pending-repository-add" / (plan_id + ".json")
        if matched:
            if len(matched) != 1 or matched[0].repository_id.casefold() != plan_id.casefold() or matched[0].repository.casefold() != repository.casefold():
                raise ValueError("repository identity conflicts with existing registry")
            existing = matched[0]
            for explicit, expected in (
                (work_dir, existing.work), (control_dir, existing.control),
                (checkpoints_dir, existing.checkpoints),
            ):
                if explicit is not None and Path(explicit).expanduser().resolve() != expected:
                    raise ValueError("retry paths differ from existing registration")
            if existing.agent_binding != cat_record.agent_binding:
                raise ValueError("registered binding differs from canonical policy")
            _ensure_checkout(existing.work, existing, "work")
            _ensure_checkout(existing.control, existing, "control")
            validate_repository_control_binding(
                repository_id=existing.repository_id, repository=existing.repository,
                expected_agent_binding=cat_record.agent_binding,
                control_dir=existing.control, catalog_path=catalog_path,
            )
            prepare_repository_state(installation_state, existing.repository_id, create=False)
            # Registry was committed before journal completion. If the process
            # died between those durable writes, finish ONLY the matching
            # prepared intent on explicit retry (no replay of any Git/checkout).
            journal = _read_pending_journal(journal_path)
            if journal is not None:
                expected_existing = {
                    "version": 1, "repository_id": existing.repository_id,
                    "repository": existing.repository,
                    "work": str(existing.work), "control": str(existing.control),
                    "checkpoints": str(existing.checkpoints),
                    "agent_binding": existing.agent_binding,
                }
                _require_matching_journal(journal, expected_existing, existing.work_mode)
                if journal["state"] == "prepared":
                    _atomic_json(journal_path, {**journal, "state": "committed"})
            return {"status": "already_registered", "repository_id": existing.repository_id,
                    "work_mode": existing.work_mode, "changes_applied": False}

        plan = plan_repository_add(
            repository, repository_id=repository_id, work_dir=work_dir,
            control_dir=control_dir, checkpoints_dir=checkpoints_dir,
            registry_path=target, catalog_path=catalog_path,
            home=resolved_home, cwd=cwd,
        )

        expected = {
            "version": 1, "repository_id": plan["repository_id"],
            "repository": plan["repository"], "work": plan["work"]["path"],
            "control": plan["control"]["path"], "checkpoints": plan["checkpoints"]["path"],
            "agent_binding": cat_record.agent_binding,
        }
        journal = _read_pending_journal(journal_path)
        if journal is not None:
            work_mode = journal.get("work_mode")
            if work_mode not in ("attached", "managed"):
                raise ValueError("invalid pending repo-add work mode")
            _require_matching_journal(journal, expected, work_mode)
        else:
            work_mode = plan["work"]["planned_mode"]
            journal = {**expected, "work_mode": work_mode, "state": "prepared"}
            _atomic_json(journal_path, journal)
        selected = repository_from_dict({
            "id": plan["repository_id"], "repository": repository,
            "control_dir": plan["control"]["path"], "work_dir": plan["work"]["path"],
            "checkpoints_dir": plan["checkpoints"]["path"],
            "work_mode": work_mode, "agent_binding": cat_record.agent_binding,
        }, home=resolved_home)
        validate_repository_set([*prior, selected], allow_empty=True)
        if work_mode == "attached":
            # A linked/unborn checkout can pass the dry-run origin check but
            # cannot safely run our self-contained snapshot executor.
            from local_agent.foundation.attached_checkout import _validate_git_directory

            _validate_git_directory(selected.work)
            _git(["rev-parse", "--verify", "HEAD"], cwd=selected.work)
            if not _git(["branch", "--show-current"], cwd=selected.work):
                raise ValueError("attached checkout requires a named current branch")
        if selected.work.exists():
            _ensure_checkout(selected.work, selected, "work")
            if work_mode == "managed":
                status = _git(["status", "--porcelain"], cwd=selected.work)
                if status:
                    raise ValueError("reused managed checkout became dirty; refuse automatic cleanup")
        else:
            if work_mode != "managed":
                raise ValueError("attached user checkout disappeared")
            _clone_to_destination(selected, selected.work, "work", catalog_path=catalog_path)
        if selected.control.exists():
            _ensure_checkout(selected.control, selected, "control")
        else:
            _clone_to_destination(
                selected, selected.control, "control",
                catalog_path=catalog_path,
            )
        validate_repository_control_binding(
            repository_id=selected.repository_id, repository=selected.repository,
            expected_agent_binding=cat_record.agent_binding,
            control_dir=selected.control, catalog_path=catalog_path,
        )
        selected.checkpoints.mkdir(parents=True, exist_ok=True)
        prepare_repository_state(installation_state, selected.repository_id, create=True)
        if target.read_bytes() != baseline:
            raise RuntimeError("repository registry changed concurrently; retry after reconciliation")
        if catalog_path is not None:
            fresh = catalog_record_for_repository(plan_id, repository, path=catalog_path)
            if fresh != cat_record:
                raise RuntimeError("canonical binding changed during registration")
        payload["repositories"].append(_as_registry_entry(selected))
        _atomic_json(target, payload)
        _atomic_json(journal_path, {**journal, "state": "committed"})
        return {
            "status": "registered", "repository_id": selected.repository_id,
            "work_mode": work_mode, "agent_binding": cat_record.agent_binding,
            "changes_applied": True,
        }
