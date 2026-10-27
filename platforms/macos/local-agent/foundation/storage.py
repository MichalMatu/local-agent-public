#!/usr/bin/env python3
"""Storage and resilient Git policy for local-agent repository workspaces."""
from __future__ import annotations

import os
import re
import stat
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from local_agent.foundation.git_lock_recovery import recover_preboot_shallow_lock

CONTROL_HISTORY_DEPTH = 256
CONTROL_HISTORY_WARNING_COMMITS = CONTROL_HISTORY_DEPTH * 2
CONTROL_HISTORY_WARNING_BYTES = 64 * 1024**2
CONTROL_WORKTREE_WARNING_BYTES = 256 * 1024**2
WORKTREE_WARNING_BYTES = 2 * 1024**3
DIAGNOSTIC_FILE_LIMIT = 100_000
CONTROL_SPARSE_PATHS = (".agent",)
CONTROL_RECOVERABLE_DIRS = (
    ".agent/status",
    ".agent/runs",
    ".agent/results",
    ".agent/conversation/results",
    ".agent/daemon/acks",
)
CONTROL_RUNTIME_TASK_PREFIX = ".agent/tasks/"
_HOST_TASK_NAMESPACE_RE = re.compile(r"\.agent/hosts/([a-z][a-z0-9-]{2,63})/tasks\Z")
_HOST_SUPERVISOR_TASKS_RE = re.compile(
    r"\.agent/hosts/[a-z][a-z0-9-]{2,63}/supervisor/"
    r"[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}/tasks\Z"
)
CONTROL_RECOVERABLE_UNTRACKED_BASENAMES = frozenset({".DS_Store"})
GIT_NETWORK_RETRY_DELAYS = (2.0,)
GIT_NETWORK_ATTEMPT_TIMEOUT_MAX = 20
CONTROL_GIT_SLOW_RETRY_TIMEOUT_MAX = 45
CONTROL_GIT_CIRCUIT_OPEN_SECONDS = 30.0
TRANSIENT_GIT_NETWORK_MARKERS = (
    "connection closed by",
    "connection reset by peer",
    "connection timed out",
    "operation timed out",
    "network is unreachable",
    "could not resolve host",
    "could not resolve hostname",
    "remote end hung up unexpectedly",
    "ssh_exchange_identification: connection closed",
    "kex_exchange_identification: read: connection reset",
    "rpc failed; curl 56",
    "gnutls recv error",
    "tls connection was non-properly terminated",
    "the requested url returned error: 502",
    "the requested url returned error: 503",
    "the requested url returned error: 504",
    "curl 28",
    "curl 52",
    "curl 55",
)


@dataclass
class GitNetworkCircuitBreaker:
    """Process-local guard that suppresses repeated control-plane network stalls."""

    open_seconds: float = CONTROL_GIT_CIRCUIT_OPEN_SECONDS
    open_until: float = 0.0

    def retry_after(self, now: float) -> float:
        return max(0.0, self.open_until - now)

    def open(self, now: float) -> None:
        self.open_until = now + self.open_seconds

    def reset(self) -> None:
        self.open_until = 0.0


CONTROL_GIT_NETWORK_CIRCUIT = GitNetworkCircuitBreaker()


def bounded_control_pull_args(branch: str) -> list[str]:
    """Git arguments that keep control history bounded and remote ACK refs exact."""
    remote_tracking = f"refs/remotes/origin/{branch}"
    return [
        "pull",
        "--rebase",
        "--depth",
        str(CONTROL_HISTORY_DEPTH),
        "--no-tags",
        "origin",
        f"+refs/heads/{branch}:{remote_tracking}",
    ]


def is_transient_git_network_failure(result: dict[str, Any]) -> bool:
    """Return True only for failures that look like temporary network transport errors."""
    if int(result.get("exit_code", 0)) == 0:
        return False
    if bool(result.get("network_circuit_open")):
        return True
    if bool(result.get("timed_out")):
        return True
    output = str(result.get("output", "")).lower()
    return any(marker in output for marker in TRANSIENT_GIT_NETWORK_MARKERS)


def git_failure_diagnostic(result: dict[str, Any]) -> str:
    """Return non-empty bounded diagnostics even when Git emitted no text."""
    output = str(result.get("output", "")).strip()
    if output:
        return output

    details = [f"exit_code={int(result.get('exit_code', 1))}"]
    if bool(result.get("timed_out")):
        details.append("timed_out=true")
    if bool(result.get("background_process_leak")):
        details.append("background_process_leak=true")
    failure_reason = str(result.get("failure_reason", "")).strip()
    if failure_reason:
        details.append(f"failure_reason={failure_reason}")
    elapsed = result.get("elapsed_seconds")
    if isinstance(elapsed, (int, float)):
        details.append(f"elapsed_seconds={float(elapsed):.3f}")
    return "git failed without output (" + ", ".join(details) + ")"


def run_git_with_network_retry(
    core_module: Any,
    args: list[str],
    cwd: Path,
    *,
    timeout: int = 120,
    retry_delays: tuple[float, ...] = GIT_NETWORK_RETRY_DELAYS,
    log_commands: bool = True,
    environment: Mapping[str, str] | None = None,
    circuit_breaker: GitNetworkCircuitBreaker | None = None,
    slow_retry_timeout: int | None = None,
) -> dict[str, Any]:
    """Run one bounded Git command, retrying only transient transport failures.

    Git transport attempts are capped at 20 seconds even when a legacy caller asks
    for a larger timeout. The default policy performs one retry after 2 seconds;
    higher-level scheduler backoff owns longer recovery. An optional process-local
    circuit breaker suppresses repeated network calls after an exhausted transient
    failure. The control sync may allow one 45-second retry *only after* the
    initial 20-second attempt timed out. Authentication and other deterministic Git errors are
    returned immediately. Silent terminal failures receive synthetic diagnostics.
    """
    current = time.monotonic()
    if circuit_breaker is not None:
        retry_after = circuit_breaker.retry_after(current)
        if retry_after > 0:
            return {
                "exit_code": 75,
                "output": f"Git network circuit open; retry after {retry_after:.1f}s",
                "network_circuit_open": True,
            }

    if slow_retry_timeout is not None and (
        isinstance(slow_retry_timeout, bool)
        or not isinstance(slow_retry_timeout, int)
        or not GIT_NETWORK_ATTEMPT_TIMEOUT_MAX <= slow_retry_timeout <= CONTROL_GIT_SLOW_RETRY_TIMEOUT_MAX
    ):
        raise ValueError("slow Git retry timeout exceeds bounded control policy")
    effective_timeout = min(timeout, GIT_NETWORK_ATTEMPT_TIMEOUT_MAX)
    result: dict[str, Any] = {}
    previous_timed_out = False
    for attempt in range(len(retry_delays) + 1):
        attempt_timeout = (
            min(timeout, slow_retry_timeout)
            if attempt > 0 and previous_timed_out and slow_retry_timeout is not None
            else effective_timeout
        )
        process_kwargs: dict[str, Any] = {
            "timeout": attempt_timeout,
            "log_commands": log_commands,
        }
        if environment is not None:
            process_kwargs["environment"] = environment
        result = core_module.process(args, cwd, **process_kwargs)
        if result["exit_code"] == 0:
            if circuit_breaker is not None:
                circuit_breaker.reset()
            return result
        transient = is_transient_git_network_failure(result)
        previous_timed_out = bool(result.get("timed_out"))
        if not transient:
            if circuit_breaker is not None:
                circuit_breaker.reset()
            if not str(result.get("output", "")).strip():
                result = dict(result)
                result["output"] = git_failure_diagnostic(result)
            return result
        if attempt >= len(retry_delays):
            if not str(result.get("output", "")).strip():
                result = dict(result)
                result["output"] = git_failure_diagnostic(result)
            if circuit_breaker is not None:
                circuit_breaker.open(time.monotonic())
                logger = getattr(core_module, "log", None)
                if callable(logger):
                    logger(
                        "Git network circuit opened after exhausted transient failure; "
                        f"suppressing probes for {circuit_breaker.open_seconds:g}s"
                    )
            return result

        delay = retry_delays[attempt]
        logger = getattr(core_module, "log", None)
        if callable(logger):
            logger(
                f"transient Git network failure; retrying in {delay:g}s "
                f"(attempt {attempt + 2}/{len(retry_delays) + 1})"
            )
        time.sleep(delay)
    return result


def require_control_rebase_clear(control: Path) -> None:
    """Fail closed on interrupted Git metadata before any control-branch sync.

    A clean worktree is not enough: a past pull --rebase can leave Git's
    rebase-merge/rebase-apply state after a crash. Never automatically abort,
    skip, clean or delete that state: there may be unpublished local commits.
    This is a read-only preflight, safe for Mac, Linux and Pi headless.
    """
    checkout = Path(control)
    dotgit = checkout / ".git"
    if dotgit.is_symlink():
        raise RuntimeError("control checkout .git symlink requires manual inspection")
    if dotgit.is_dir():
        gitdir = dotgit
    elif dotgit.is_file():
        # Git worktrees use a tiny '.git' text file that points at the actual
        # metadata directory. Do not call 'git' or mutate the checkout.
        if dotgit.stat().st_size > 4096:
            raise RuntimeError("unexpected control checkout .git file")
        try:
            record = dotgit.read_text(encoding="utf-8").strip()
        except (OSError, UnicodeError) as exc:
            raise RuntimeError("unable to inspect control checkout .git file") from exc
        if not record.startswith("gitdir: ") or "\n" in record:
            raise RuntimeError("unexpected control checkout .git file")
        target = Path(record[len("gitdir: "):])
        gitdir = target if target.is_absolute() else checkout / target
        # A stale worktree pointer is not a clean Git state. Without this
        # check the missing target silently made both rebase probes false.
        if gitdir.is_symlink() or not gitdir.is_dir():
            raise RuntimeError(
                "control checkout .git gitdir target missing or symlinked; "
                "refusing control pull/publish pending manual inspection"
            )
    else:
        # An uninitialized checkout (or mocked checkout in unit tests)
        # has no rebase state to detect; normal Git admission still fails.
        return
    for name in ("rebase-merge", "rebase-apply"):
        state = gitdir / name
        if state.exists() or state.is_symlink():
            raise RuntimeError(
                f"control checkout has unfinished Git {name}; "
                "refusing control pull/publish before preserving local "
                "commits and inspecting rebase state. Do not delete "
                "Git metadata or run rebase --abort automatically."
            )


def _control_status_entries(core_module: Any) -> tuple[tuple[str, str], ...]:
    """Return exact porcelain status entries without Git path quoting."""
    status = core_module.process(
        ["git", "status", "--porcelain=v1", "-z", "--untracked-files=all"],
        core_module.CONTROL,
        timeout=30,
        log_commands=False,
    )
    if status["exit_code"] != 0:
        raise RuntimeError(git_failure_diagnostic(status))

    fields = str(status.get("output", "")).split("\0")
    entries: list[tuple[str, str]] = []
    index = 0
    while index < len(fields):
        record = fields[index]
        if not record:
            index += 1
            continue
        if len(record) < 4 or record[2] != " ":
            raise RuntimeError(f"invalid git status porcelain record: {record!r}")
        code = record[:2]
        path = record[3:]
        if not path:
            raise RuntimeError("invalid git status porcelain path")
        entries.append((code, path))

        if "R" in code or "C" in code:
            index += 1
            if index >= len(fields) or not fields[index]:
                raise RuntimeError("incomplete git status rename/copy record")
            entries.append((code, fields[index]))
        index += 1
    return tuple(entries)


def _control_dirty_paths(core_module: Any) -> tuple[str, ...]:
    return tuple(sorted({path for _code, path in _control_status_entries(core_module)}))


def _recoverable_control_paths(tasks_dir: str) -> tuple[tuple[str, ...], bool]:
    """Return only runtime paths owned by the selected mailbox.

    A host worker must not repair another host's checkout changes or the
    original MacBook's legacy queue. In particular, host-specific tasks are
    never treated as runtime-GC deletions: host GC is not enabled.
    """
    if tasks_dir == ".agent/tasks":
        return CONTROL_RECOVERABLE_DIRS, True
    match = _HOST_TASK_NAMESPACE_RE.fullmatch(tasks_dir)
    host_supervisor = _HOST_SUPERVISOR_TASKS_RE.fullmatch(tasks_dir)
    if match is None and host_supervisor is None:
        raise ValueError("invalid selected control task mailbox")
    namespace = tasks_dir.removesuffix("/tasks")
    suffixes = (
        "results", "runs", "status", "status.json", "daemon/acks", "acks",
    )
    if host_supervisor is not None:
        # Host-only command results are independently admitted and published
        # in the supervisor's installation-scoped transport. Never repair
        # another host or broaden project result recovery by this addition.
        suffixes += ("task-results",)
    return (
        tuple(f"{namespace}/{suffix}" for suffix in suffixes),
        False,
    )


def _recoverable_control_path(path: str, directories: tuple[str, ...]) -> bool:
    return any(
        path == directory or path.startswith(directory + "/")
        for directory in directories
    )


def _recoverable_runtime_task_deletion(code: str, path: str) -> bool:
    """Recover only task deletions that an interrupted runtime GC could have made."""
    # Legacy GC removes only one-level, bounded JSON task IDs. A deleted
    # nested path or unrelated file below tasks/ is not evidence of runtime GC.
    suffix = path.removeprefix(CONTROL_RUNTIME_TASK_PREFIX)
    return (
        code in {"D", " D", "D "}
        and path.startswith(CONTROL_RUNTIME_TASK_PREFIX)
        and re.fullmatch(r"[A-Za-z0-9._-]{1,200}\.json", suffix) is not None
    )


def _recoverable_untracked_control_noise(code: str, path: str) -> bool:
    return (
        code == "??"
        and Path(path).name in CONTROL_RECOVERABLE_UNTRACKED_BASENAMES
    )


def _exclude_recurring_finder_metadata(control: Path) -> None:
    """Best-effort local Git exclude, never a tracked or global Git config edit.

    Finder can recreate .DS_Store after each cleanup. Suppress subsequent
    Git status noise in this *one* checkout instead of cleaning it every poll.
    Only touch a regular, user-owned .git/info/exclude in a normal Git checkout.
    A nonstandard/readonly Git directory simply keeps the safe cleanup fallback.
    """
    git_dir = control / ".git"
    info_dir = git_dir / "info"
    try:
        for directory in (git_dir, info_dir):
            if directory.is_symlink():
                return
            details = directory.stat()
            if (not stat.S_ISDIR(details.st_mode)
                    or (os.name == "posix" and details.st_uid != os.getuid())):
                return
        exclude = info_dir / "exclude"
        if exclude.is_symlink():
            return
        existing = exclude.read_bytes() if exclude.exists() else b""
        if len(existing) > 65536 or b"\0" in existing:
            return
        if b".DS_Store" in existing.splitlines():
            return
        flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(exclude, flags, 0o600)
        try:
            details = os.fstat(fd)
            if (not stat.S_ISREG(details.st_mode)
                    or (os.name == "posix" and details.st_uid != os.getuid())
                    or details.st_size > 65536):
                return
            prefix = b"" if not existing or existing.endswith(b"\n") else b"\n"
            payload = prefix + b".DS_Store\n"
            while payload:
                payload = payload[os.write(fd, payload):]
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError:
        # Local excludes are an optimization; failure must not stop safe sync.
        return


def recover_daemon_owned_control_changes(
    core_module: Any, *, tasks_dir: str = ".agent/tasks",
) -> None:
    """Recover only the selected agent's interrupted control publications."""
    recoverable_dirs, allow_legacy_task_gc = _recoverable_control_paths(tasks_dir)
    entries = _control_status_entries(core_module)
    if not entries:
        return

    unexpected = tuple(
        path
        for code, path in entries
        if not _recoverable_control_path(path, recoverable_dirs)
        and not (allow_legacy_task_gc and _recoverable_runtime_task_deletion(code, path))
        and not _recoverable_untracked_control_noise(code, path)
    )
    if unexpected:
        raise RuntimeError(
            "control checkout has unexpected local changes: "
            + ", ".join(unexpected)
        )

    # Finder's harmless metadata must not produce one log line per Git poll.
    # Preserve diagnostics for interrupted real daemon publications.
    meaningful = tuple(sorted({
        path for code, path in entries
        if not _recoverable_untracked_control_noise(code, path)
    }))
    logger = getattr(core_module, "log", None)
    if meaningful and callable(logger):
        logger(
            "recovering safe control checkout changes before sync: "
            + ", ".join(meaningful)
        )

    tracked = tuple(sorted({path for code, path in entries if code != "??"}))
    untracked = tuple(sorted({path for code, path in entries if code == "??"}))

    if tracked:
        restore = core_module.process(
            [
                "git",
                "restore",
                "--source=HEAD",
                "--staged",
                "--worktree",
                "--",
                *tracked,
            ],
            core_module.CONTROL,
            timeout=30,
            log_commands=False,
        )
        if restore["exit_code"] != 0:
            raise RuntimeError(git_failure_diagnostic(restore))

    if untracked:
        clean = core_module.process(
            ["git", "clean", "-fd", "--", *untracked],
            core_module.CONTROL,
            timeout=30,
            log_commands=False,
        )
        if clean["exit_code"] != 0:
            raise RuntimeError(git_failure_diagnostic(clean))

    if any(_recoverable_untracked_control_noise(code, path) for code, path in entries):
        # Install the exclude *after* Git cleans this instance, before the
        # final status check: Finder might recreate the same file immediately.
        _exclude_recurring_finder_metadata(Path(core_module.CONTROL))
    remaining = _control_dirty_paths(core_module)
    if remaining:
        raise RuntimeError(
            "control checkout remained dirty after daemon-owned recovery: "
            + ", ".join(remaining)
        )


def interrupted_control_rebase_metadata(control: Path) -> dict[str, str] | None:
    """Read validated managed-clone sequencer identity without Git effects."""
    try:
        require_control_rebase_clear(control)
        return None
    except RuntimeError as original_error:
        checkout = Path(control)
        gitdir = checkout / ".git"
        # Automatic recovery is deliberately restricted to managed clones.
        if gitdir.is_symlink() or not gitdir.is_dir():
            raise
        states = [gitdir / name for name in ("rebase-merge", "rebase-apply")
                  if (gitdir / name).exists() or (gitdir / name).is_symlink()]
        if len(states) != 1 or states[0].is_symlink() or not states[0].is_dir():
            raise
        state = states[0]
        values = {}
        for name in ("head-name", "orig-head", "onto"):
            path = state / name
            if path.is_symlink() or not path.is_file() or path.stat().st_size > 256:
                raise RuntimeError(f"{original_error}; untrusted recovery metadata")
            values[name] = path.read_text(encoding="ascii").strip()
        if any(re.fullmatch(r"[0-9a-f]{40}", values[name]) is None
               for name in ("orig-head", "onto")):
            raise RuntimeError(f"{original_error}; invalid recovery commit identity")
        return values


def recover_interrupted_control_rebase(
    core_module: Any, *, tasks_dir: str = ".agent/tasks",
) -> bool:
    """Rebuild disposable mailbox state after preserving its generated commits.

    Callers already own the mailbox execution/publication lease. Only Git's
    checkout is disposable: claims, results and execution ledgers are not.
    An immutable per-tip recovery ref retains each branch tip even if Git detached HEAD.
    Unexpected paths/metadata remain an explicit failure of this mailbox.
    """
    values = interrupted_control_rebase_metadata(core_module.CONTROL)
    if values is None:
        return False
    checkout = Path(core_module.CONTROL)
    branch = core_module.CONTROL_BRANCH
    if values["head-name"] != f"refs/heads/{branch}":
        raise RuntimeError("unfinished Git rebase targets a different mailbox branch")
    recover_daemon_owned_control_changes(core_module, tasks_dir=tasks_dir)
    def checked(args: list[str]) -> dict:
        result = core_module.process(args, checkout, timeout=30, log_commands=False)
        if result["exit_code"] != 0:
            raise RuntimeError(git_failure_diagnostic(result))
        return result

    base = str(checked(["git", "merge-base", values["onto"],
                        f"refs/heads/{branch}"])["output"]).strip()
    # Preserve the distinction between a runtime-GC task deletion and an
    # owner-created/modified task. The old --name-only guard rejected harmless
    # tracked GC deletions and could strand a project in rebase recovery.
    raw_changes = str(checked([
        "git", "diff", "--no-renames", "--name-status", "-z", base,
        f"refs/heads/{branch}",
    ])["output"]).split("\0")
    if raw_changes[-1] != "" or len(raw_changes) % 2 != 1:
        raise RuntimeError("unexpected Git changed-path status during control recovery")
    generated, allow_legacy_task_gc = _recoverable_control_paths(tasks_dir)
    for code, path in zip(raw_changes[0:-1:2], raw_changes[1:-1:2]):
        if (
            not path
            or not (
                _recoverable_control_path(path, generated)
                or (
                    allow_legacy_task_gc
                    and code == "D"
                    and _recoverable_runtime_task_deletion(code, path)
                )
            )
        ):
            raise RuntimeError(
                "unfinished Git rebase includes non-publication commits; recovery refused"
            )
    fetched = run_git_with_network_retry(
        core_module,
        ["git", "fetch", "--depth", str(CONTROL_HISTORY_DEPTH), "--no-tags", "origin",
         f"+refs/heads/{branch}:refs/remotes/origin/{branch}"], checkout,
    )
    if fetched["exit_code"] != 0:
        raise RuntimeError(git_failure_diagnostic(fetched))
    tip = str(checked(["git", "rev-parse", "--verify", f"refs/heads/{branch}"])["output"]).strip()
    if re.fullmatch(r"[0-9a-f]{40}", tip) is None:
        raise RuntimeError("invalid interrupted control branch tip")
    recovery_ref = f"refs/local-agent/recovery/interrupted-rebase-{tip}"
    existing = core_module.process(
        ["git", "rev-parse", "--verify", "--quiet", recovery_ref],
        checkout, timeout=30, log_commands=False,
    )
    if existing["exit_code"] == 1:
        checked(["git", "update-ref", recovery_ref, tip, "0" * 40])
    elif existing["exit_code"] != 0 or str(existing["output"]).strip() != tip:
        raise RuntimeError("existing interrupted-rebase recovery ref differs; recovery refused")
    for args in (
        ["git", "rebase", "--quit"],
        ["git", "checkout", branch],
        ["git", "reset", "--hard", f"refs/remotes/origin/{branch}"],
    ):
        checked(args)
    require_control_rebase_clear(checkout)
    logger = getattr(core_module, "log", None)
    if callable(logger):
        logger("rebuilt interrupted control mailbox; generated commits checkpointed, execution ledgers retained")
    return True


def sync_control(core_module: Any, *, tasks_dir: str = ".agent/tasks") -> None:
    """Synchronize one mailbox without cleaning another host's runtime artifacts."""
    _, legacy_mailbox = _recoverable_control_paths(tasks_dir)
    with core_module.CONTROL_GIT_LOCK:
        recover_preboot_shallow_lock(core_module)
        recover_interrupted_control_rebase(core_module, tasks_dir=tasks_dir)
        require_control_rebase_clear(core_module.CONTROL)
        recover_daemon_owned_control_changes(core_module, tasks_dir=tasks_dir)
        branch = core_module.process(
            ["git", "symbolic-ref", "--quiet", "--short", "HEAD"],
            core_module.CONTROL,
            timeout=30,
            log_commands=False,
        )
        if (
            branch["exit_code"] != 0
            or str(branch.get("output", "")).strip() != core_module.CONTROL_BRANCH
        ):
            result = core_module.process(
                ["git", "checkout", core_module.CONTROL_BRANCH],
                core_module.CONTROL,
            )
            if result["exit_code"] != 0:
                raise RuntimeError(result["output"])

        result = run_git_with_network_retry(
            core_module,
            ["git", *bounded_control_pull_args(core_module.CONTROL_BRANCH)],
            core_module.CONTROL,
            circuit_breaker=CONTROL_GIT_NETWORK_CIRCUIT,
            slow_retry_timeout=CONTROL_GIT_SLOW_RETRY_TIMEOUT_MAX,
        )
        if result["exit_code"] != 0:
            raise RuntimeError(result["output"])

    if not legacy_mailbox:
        # Legacy GC deletes legacy tasks/results and can compact control history.
        # A host-only worker must never perform those repository-global writes.
        return
    try:
        from local_agent.repository.cleanup import prune_control_runtime

        prune_control_runtime(core_module)
    except Exception as exc:
        logger = getattr(core_module, "log", None)
        if callable(logger):
            logger(f"runtime GC skipped after sync: {type(exc).__name__}: {exc}")


def is_concurrent_control_push_rejection(output: str) -> bool:
    """Recognize stale remote HEAD races, not arbitrary remote rejections.

    A non-fast-forward response or Git server compare-and-swap rejection is
    retryable only by pulling/rebasing the same local commit. Authorization,
    protected-branch hooks, pack failures and ambiguous errors are not races.
    """
    failure = output.lower()
    if "non-fast-forward" in failure or "fetch first" in failure:
        return True
    return (
        "cannot lock ref" in failure
        and "is at " in failure
        and "but expected " in failure
        and ("[remote rejected]" in failure or "remote rejected" in failure)
    )


def publish_control_commit(
    core_module: Any,
    *,
    timeout: int = 180,
    attempts: int = 2,
    log_commands: bool = False,
    post_pull_validate: Callable[[], None] | None = None,
) -> bool:
    """Rebase and push an existing commit, boundedly reconciling concurrent writers."""
    if isinstance(attempts, bool) or not isinstance(attempts, int) or not 1 <= attempts <= 3:
        raise ValueError("control publication attempts must be in range 1..3")
    for attempt in range(attempts):
        require_control_rebase_clear(core_module.CONTROL)
        pull = run_git_with_network_retry(
            core_module,
            ["git", *bounded_control_pull_args(core_module.CONTROL_BRANCH)],
            core_module.CONTROL,
            timeout=timeout,
            log_commands=log_commands,
        )
        if pull["exit_code"] != 0:
            raise RuntimeError(git_failure_diagnostic(pull))
        if post_pull_validate is not None:
            post_pull_validate()
        push = run_git_with_network_retry(
            core_module,
            ["git", "push", "origin", core_module.CONTROL_BRANCH],
            core_module.CONTROL,
            timeout=timeout,
            log_commands=log_commands,
        )
        if push["exit_code"] == 0:
            return True
        output = str(push.get("output", "")).lower()
        competing_push = is_concurrent_control_push_rejection(output)
        if not competing_push or attempt == attempts - 1:
            raise RuntimeError(git_failure_diagnostic(push))
        logger = getattr(core_module, "log", None)
        if callable(logger):
            logger(
                "control Git publication conflict: concurrent writer; "
                f"rebasing for attempt {attempt + 2}/{attempts}"
            )
    return False


def git_bool(core_module: Any, path: Path, args: list[str]) -> bool | None:
    """Read a Git boolean-like diagnostic without raising on unsupported settings."""
    result = core_module.process(
        ["git", *args],
        path,
        timeout=30,
        log_commands=False,
    )
    if result["exit_code"] != 0:
        return None
    value = str(result.get("output", "")).strip().lower()
    if value in {"true", "yes", "on", "1"}:
        return True
    if value in {"false", "no", "off", "0"}:
        return False
    return None
