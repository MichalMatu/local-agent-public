"""Fail-closed recovery for orphaned repository execution-lease holders."""

from __future__ import annotations

import fcntl
import os
import shutil
import signal
import time
from collections.abc import Callable, Iterable
from pathlib import Path

from local_agent.foundation.process import (
    LEASE_FDS_ENV,
    LEASE_KEYS_DIGEST_ENV,
    RESOURCE_LEASE_FDS_ENV,
    execution_lease_path,
    run_argv_bounded,
)
from local_agent.repository.context import RepositoryContext, repository_lease_keys

DEFAULT_TERMINATION_GRACE_SECONDS = 1.0
HOLDER_SCAN_TIMEOUT_SECONDS = 3.0
HOLDER_SCAN_OUTPUT_LIMIT = 16384
# Darwin's fileglob flag historically named FHASLOCK and now FWASLOCKED.
# It marks the fileglob that acquired an advisory lock; descriptors inherited
# through fork/exec share that fileglob while an unrelated open() does not.
DARWIN_FHASLOCK = 0x00004000


def repository_lease_paths(
    repositories: Iterable[RepositoryContext],
    *,
    state_dir: Path,
) -> tuple[Path, ...]:
    """Return every configured repository identity-lock path exactly once."""
    lock_dir = state_dir / "locks" / "repositories"
    paths = {
        execution_lease_path(lock_dir, key)
        for repository in repositories
        for key in repository_lease_keys(repository)
    }
    return tuple(sorted(paths))


def repository_leases_busy(paths: Iterable[Path]) -> bool:
    """Probe repository locks without rewriting their ownership metadata."""
    for path in paths:
        path.parent.mkdir(parents=True, exist_ok=True)
        handle = path.open("a+", encoding="utf-8")
        try:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return True
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()
    return False


def _proc_holder_pids(paths: tuple[Path, ...]) -> set[int]:
    """Resolve processes whose matching Linux fdinfo still carries a FLOCK.

    `/proc/locks` records the PID that originally acquired a BSD flock. After
    that descriptor is inherited and the original process closes its copy, the
    recorded PID may be stale while a descendant still keeps the open-file
    description locked. Per-process fdinfo follows the inherited descriptor and
    therefore identifies the process that can actually keep the lease alive.
    """
    targets: set[tuple[int, int]] = set()
    for path in paths:
        try:
            stat = path.stat()
        except FileNotFoundError:
            continue
        targets.add((stat.st_dev, stat.st_ino))
    if not targets:
        return set()

    holders: set[int] = set()
    proc_root = Path("/proc")
    for process_dir in proc_root.iterdir():
        if not process_dir.name.isdigit():
            continue
        pid = int(process_dir.name)
        fd_dir = process_dir / "fd"
        try:
            descriptors = tuple(fd_dir.iterdir())
        except (FileNotFoundError, NotADirectoryError, PermissionError, OSError):
            continue
        for descriptor in descriptors:
            try:
                stat = descriptor.stat()
            except (FileNotFoundError, PermissionError, OSError):
                continue
            if (stat.st_dev, stat.st_ino) not in targets:
                continue
            try:
                info = (process_dir / "fdinfo" / descriptor.name).read_text(
                    encoding="utf-8"
                )
            except (FileNotFoundError, PermissionError, OSError):
                continue
            if any(
                line.startswith("lock:") and " FLOCK " in line
                for line in info.splitlines()
            ):
                holders.add(pid)
                break
    return holders


def _lsof_holder_pids(
    paths: tuple[Path, ...],
    *,
    log: Callable[[str], None],
) -> set[int]:
    """Resolve Darwin holders using both fileglob and current-lock evidence.

    macOS `lsof` may report the vnode's lock status for another process that has
    merely opened the same path. Requiring the per-fileglob FHASLOCK/FWASLOCKED
    bit as well prevents such observers from becoming recovery victims. An
    inherited daemon shares the original locked fileglob, so it keeps the bit.
    """
    executable = shutil.which("lsof")
    if executable is None:
        raise RuntimeError("cannot inspect orphaned lease holders: lsof is unavailable")

    env = os.environ.copy()
    for name in (LEASE_FDS_ENV, LEASE_KEYS_DIGEST_ENV, RESOURCE_LEASE_FDS_ENV):
        env.pop(name, None)

    holders: set[int] = set()
    for path in paths:
        if not path.exists():
            continue
        result = run_argv_bounded(
            [executable, "-n", "+fG", "-FpfGl", "--", str(path)],
            cwd=path.parent,
            env=env,
            timeout=HOLDER_SCAN_TIMEOUT_SECONDS,
            output_limit=HOLDER_SCAN_OUTPUT_LIMIT,
            log=log,
        )
        exit_code = int(result.get("exit_code", 1))
        if exit_code not in (0, 1):
            raise RuntimeError(
                f"lease-holder inspection failed path={path} exit={exit_code}"
            )

        current_pid: int | None = None
        in_file = False
        fileglob_has_lock = False
        current_lock = False
        for line in str(result.get("output", "")).splitlines():
            if line.startswith("p") and line[1:].isdigit():
                current_pid = int(line[1:])
                in_file = False
                fileglob_has_lock = False
                current_lock = False
                continue
            if line.startswith("f"):
                in_file = True
                fileglob_has_lock = False
                current_lock = False
                continue
            if not in_file or current_pid is None:
                continue
            if line.startswith("G"):
                try:
                    # Darwin lsof emits fileglob and descriptor flag masks as
                    # `G0x<fileglob>;0x<descriptor>`. Only the first mask carries
                    # FHASLOCK/FWASLOCKED and follows the inherited fileglob.
                    flags = int(line[1:].split(";", 1)[0], 0)
                except ValueError as exc:
                    raise RuntimeError(
                        f"invalid lsof file flags for lease-holder inspection: {line!r}"
                    ) from exc
                fileglob_has_lock = bool(flags & DARWIN_FHASLOCK)
            elif line.startswith("l"):
                current_lock = bool(line[1:])

            if fileglob_has_lock and current_lock:
                holders.add(current_pid)
    return holders


def lease_holder_pids(
    paths: Iterable[Path],
    *,
    log: Callable[[str], None],
) -> set[int]:
    """Find processes that actually hold a lock on a configured repository lease."""
    resolved = tuple(dict.fromkeys(Path(path) for path in paths))
    if Path("/proc").is_dir():
        return _proc_holder_pids(resolved)
    return _lsof_holder_pids(resolved, log=log)


def _signal_process(pid: int, signum: int) -> None:
    try:
        os.kill(pid, signum)
    except ProcessLookupError:
        pass


def recover_orphaned_repository_leases(
    paths: Iterable[Path],
    *,
    log: Callable[[str], None],
    grace_seconds: float = DEFAULT_TERMINATION_GRACE_SECONDS,
) -> tuple[int, ...]:
    """Terminate stale lock holders after the supervisor and workers are stopped.

    This function is intentionally fail-closed. Callers must first stop the
    supervisor so every remaining holder is an orphaned command descendant.
    """
    resolved = tuple(dict.fromkeys(Path(path) for path in paths))
    if not resolved or not repository_leases_busy(resolved):
        return ()

    protected = {os.getpid(), os.getppid(), 0, 1}
    holders = lease_holder_pids(resolved, log=log)
    victims = tuple(sorted(pid for pid in holders if pid not in protected and pid > 1))
    if not victims:
        raise RuntimeError("repository lease is busy but no recoverable holder was found")

    log(f"recovering orphaned repository lease holders pids={list(victims)}")
    for pid in victims:
        _signal_process(pid, signal.SIGTERM)

    deadline = time.monotonic() + max(0.0, grace_seconds)
    remaining: set[int] = set(victims)
    while remaining and time.monotonic() < deadline:
        time.sleep(0.05)
        current = lease_holder_pids(resolved, log=log) - protected
        remaining.intersection_update(current)

    if remaining:
        log(f"killing orphaned repository lease holders pids={sorted(remaining)}")
        for pid in sorted(remaining):
            _signal_process(pid, signal.SIGKILL)

    verify_deadline = time.monotonic() + 1.0
    while time.monotonic() < verify_deadline:
        current = lease_holder_pids(resolved, log=log) - protected
        if not current and not repository_leases_busy(resolved):
            return victims
        time.sleep(0.05)

    current = lease_holder_pids(resolved, log=log) - protected
    if current:
        raise RuntimeError(
            f"orphaned repository lease holders survived recovery pids={sorted(current)}"
        )
    if repository_leases_busy(resolved):
        raise RuntimeError("repository lease remained busy after orphan recovery")
    return victims
