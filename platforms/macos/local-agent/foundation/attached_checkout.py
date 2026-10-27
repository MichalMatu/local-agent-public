"""Isolated snapshot for an explicitly attached, user-maintained Git checkout.

The source is read-only: tasks run in a private full copy (including its own
Git index/objects), never in the user's checkout. This is task isolation, NOT
an OS security sandbox for arbitrary commands containing external paths.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import subprocess
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator


def _git(path: Path, *args: str) -> str:
    env = os.environ.copy()
    for name in (
        "GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR",
        "GIT_OBJECT_DIRECTORY", "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    ):
        env.pop(name, None)
    result = subprocess.run(
        ["git", "-C", str(path), *args],
        capture_output=True, text=True, timeout=30, check=False, env=env,
    )
    if result.returncode:
        raise RuntimeError(f"attached checkout Git inspection failed: {args[0]}")
    return result.stdout.strip()


def _inside(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def _fingerprint(root: Path) -> str:
    """Hash every user-visible file, including ignored files, without following links."""
    digest = hashlib.sha256()
    for current, directories, files in os.walk(root, followlinks=False):
        parent = Path(current)
        if parent == root and ".git" in directories:
            directories.remove(".git")
        for name in sorted([*directories, *files]):
            path = parent / name
            relative = path.relative_to(root).as_posix()
            if relative == ".git" or relative.startswith(".git/"):
                continue
            if relative.endswith("/.git"):
                raise ValueError("nested Git checkout is not supported in attached mode")
            mode = path.lstat().st_mode
            digest.update(relative.encode("utf-8", "surrogateescape") + b"\0")
            if stat.S_ISLNK(mode):
                target = os.readlink(path)
                # Absolute and escaping links could write back into the source
                # or other owner files when a task follows them in the copy.
                if os.path.isabs(target) or not _inside(path.resolve(), root):
                    raise ValueError(f"unsafe external symlink in attached checkout: {relative}")
                digest.update(b"L" + os.fsencode(target) + b"\0")
            elif stat.S_ISDIR(mode):
                digest.update(b"D")
            elif stat.S_ISREG(mode):
                digest.update(b"F" + str(stat.S_IMODE(mode)).encode() + b"\0")
                with path.open("rb") as stream:
                    for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                        digest.update(chunk)
            else:
                raise ValueError(f"special file in attached checkout: {relative}")
    index = root / ".git" / "index"
    if not index.is_file() or index.is_symlink():
        raise ValueError("attached checkout requires a regular Git index")
    digest.update(b"INDEX\0" + hashlib.sha256(index.read_bytes()).digest())
    digest.update(b"HEAD\0" + _git(root, "rev-parse", "HEAD").encode())
    digest.update(b"BRANCH\0" + _git(root, "rev-parse", "--abbrev-ref", "HEAD").encode())
    return digest.hexdigest()


def _validate_git_directory(source: Path) -> None:
    git_dir = source / ".git"
    if git_dir.is_symlink() or not git_dir.is_dir():
        raise ValueError("attached mode requires a standalone .git directory (no linked worktrees)")
    if Path(_git(source, "rev-parse", "--show-toplevel")).resolve() != source:
        raise ValueError("attached checkout must be the Git toplevel")
    if (git_dir / "objects" / "info" / "alternates").exists():
        raise ValueError("attached checkout with alternate Git objects is not supported")
    for parent, dirs, files in os.walk(git_dir, followlinks=False):
        for name in (*dirs, *files):
            path = Path(parent) / name
            if path.is_symlink():
                raise ValueError("attached .git directory cannot contain symlinks")
    if _git(source, "rev-parse", "--git-common-dir") not in (".git", str(git_dir)):
        raise ValueError("attached checkout uses an external Git common directory")


@contextmanager
def isolated_attached_checkout(
    source: Path, checkpoints: Path, task_id: str,
) -> Iterator[Path]:
    """Copy and verify a stable owner checkout; never modify its files or Git index.

    Only a completed invocation removes its own private sandbox. An interrupted
    or exceptional task retains an isolated copy for explicit manual recovery.
    """
    source = source.resolve(strict=True)
    checkpoints = checkpoints.resolve(strict=False)
    if _inside(checkpoints, source) or _inside(source, checkpoints):
        raise ValueError("attached checkout and checkpoint paths must be disjoint")
    _validate_git_directory(source)
    before = _fingerprint(source)
    root = checkpoints / "attached-task-workspaces"
    root.mkdir(parents=True, exist_ok=True)
    private = Path(tempfile.mkdtemp(prefix="task-", dir=root))
    os.chmod(private, 0o700)
    work = private / "work"
    activated = False
    try:
        shutil.copytree(source, work, symlinks=True)
        if _fingerprint(work) != before or _fingerprint(source) != before:
            raise RuntimeError("attached checkout changed during snapshot; no task executed")
        _validate_git_directory(work)
        # Disable hooks/fsmonitor in the private copy, not the source.
        _git(work, "config", "--local", "core.hooksPath", str(private / "disabled-hooks"))
        _git(work, "config", "--local", "core.fsmonitor", "false")
        (private / "snapshot.json").write_text(
            json.dumps({
                "task_id": task_id,
                "source": str(source),
                "source_head": _git(source, "rev-parse", "HEAD"),
                "source_branch": _git(source, "rev-parse", "--abbrev-ref", "HEAD"),
                "fingerprint": before,
                "mode": "attached_isolated",
            }, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        activated = True
        yield work
    except BaseException:
        # A crashed/failed task may have useful results in the private copy.
        # Never cleanup its contents as if it were a disposable owner checkout.
        if not activated:
            shutil.rmtree(private)
        raise
    else:
        completed_fingerprint = _fingerprint(work)
        if completed_fingerprint == before:
            shutil.rmtree(private)
        else:
            # Keep successful edits in this isolated checkpoint for review.
            # The task can access private/ via ".."; never follow or
            # overwrite a task-supplied completion.json or symlink.
            completion_path = private / "completion.json"
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            fd = os.open(completion_path, flags, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump({
                    "version": 1,
                    "task_id": task_id,
                    "state": "completed_with_changes",
                    "source_fingerprint": before,
                    "result_fingerprint": completed_fingerprint,
                    "promotion": "manual_only",
                    "owner_reconciliation": "not_checked",
                }, handle, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())

def prepare_task_workspace(core: Any, task: dict, branch: str) -> tuple[str, str | None]:
    """Choose the legacy managed path or validate an isolated owner snapshot."""
    if core.WORK_PREPARATION_MODE == "managed":
        core.prepare_work(branch, task_id=task["id"])
        return branch, None
    if core.WORK_PREPARATION_MODE != "attached_isolated":
        raise ValueError("unknown work preparation mode")
    core.validate_branch(branch)
    current = core.process(["git", "branch", "--show-current"], core.WORK, timeout=30)
    if current["exit_code"] != 0 or not current["output"].strip():
        raise ValueError("attached task requires a named source branch")
    actual = current["output"].strip()
    if "work_branch" in task and actual != branch:
        raise ValueError("attached task branch differs from the owner's branch")
    status, diff = core.git_snapshot()
    if status["exit_code"] != 0 or diff["exit_code"] != 0:
        raise RuntimeError("could not snapshot attached worktree before task")
    return actual, _fingerprint(core.WORK)


def workspace_changed(work: Path, original: str | None, status: dict) -> bool:
    if original is None:
        return bool(status.get("output", "").strip())
    return _fingerprint(work) != original


@contextmanager
def bind_task_workspace(repository: Any, core: Any, task_id: str) -> Iterator[None]:
    """Bind only the short-lived repository worker, never a supervisor global."""
    core.WORK_PREPARATION_MODE = "managed"
    if repository.work_mode == "managed":
        yield
        return
    if repository.work_mode != "attached":
        raise ValueError("unknown repository work_mode")
    with isolated_attached_checkout(
        repository.work, repository.checkpoints, task_id,
    ) as sandbox:
        core.WORK = sandbox
        core.WORK_PREPARATION_MODE = "attached_isolated"
        try:
            yield
        finally:
            core.WORK = repository.work
            core.WORK_PREPARATION_MODE = "managed"

def execute_in_task_workspace(
    repository: Any, core: Any, task: dict, runner: Callable[..., str],
) -> str:
    with bind_task_workspace(repository, core, str(task["id"])):
        return runner(
            task, remote_daemon_status=False, remote_result_published=False,
        )
