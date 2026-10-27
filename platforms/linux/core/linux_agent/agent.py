"""Manually invoked Linux Agent pilot (never automatically installs a daemon)."""
from __future__ import annotations

from contextlib import contextmanager, closing
import fcntl
import json
import os
from pathlib import Path
import re
import stat
import threading
import time
from typing import Any, Iterator

from linux_agent.foundation.process import (
    atomic_write_text, fsync_directory, acquire_execution_leases,
    execution_lease_path,
)
from linux_agent.execution_recovery import recover_task_lease
from linux_agent.git_transport import GitError, GitTransport
from linux_agent.runtime.core import Core
from linux_agent.runtime.executor import RuntimeExecutor
from linux_agent.runtime.task_contract import task_digest, validate_task
from linux_agent.resources import MachineResourceBusy, machine_resource_lease, resource_lock_dir

ID = re.compile(r"^[A-Za-z0-9._-]{1,200}$")

def _unique_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON field: " + key)
        result[key] = value
    return result



# The copied Mac contract validates a broader task grammar than this pilot executes.
# Fail closed on anything the Linux one-shot Core would silently omit.
LINUX_HOST_FIELDS = frozenset({
    "version", "id", "target_host_id", "target_installation_id", "cwd",
    "mode", "commands", "resources", "command_timeout", "idle_timeout",
    "task_timeout", "memory_limit_mb", "output_policy",
})


def validate_linux_host_subset(task: dict[str, Any]) -> None:
    unsupported = sorted(set(task) - LINUX_HOST_FIELDS)
    if unsupported:
        raise ValueError("Linux pilot has not implemented fields: " + ", ".join(unsupported))
    if task.get("version") != 1 or task.get("mode", "commands") != "commands":
        raise ValueError("unsupported Linux host task version or mode")
    commands = task.get("commands")
    if not isinstance(commands, list) or not 1 <= len(commands) <= 32:
        raise ValueError("Linux command-only pilot requires 1..32 commands")
    policy = task.get("output_policy", "summary")
    if not isinstance(policy, str) or policy not in Core.OUTPUT_POLICIES:
        raise ValueError("unsupported Linux output_policy")
    cwd = task.get("cwd")
    if not isinstance(cwd, str) or not Path(cwd).is_absolute() or not Path(cwd).is_dir():
        raise ValueError("Linux host cwd must be an existing absolute directory")
    # Names are validated by the copied Mac task contract and acquired using
    # its exact resource-specific flock admission before any durable claim.


def validate_config(config: dict[str, Any], state: Path) -> None:
    if config.get("version") != 1:
        raise ValueError("unknown config version")
    for field in ("host_id", "installation_id"):
        v = config.get(field)
        if not isinstance(v, str) or not ID.fullmatch(v):
            raise ValueError(f"invalid {field}")
    if config.get("repository") is None or not isinstance(config["repository"], str):
        raise ValueError("repository missing")
    if config.get("branch") != "agent-control":
        raise ValueError("branch mismatch")
    if config.get("host_intake") != {"enabled": True, "authorization": "github_writers"}:
        raise ValueError("host task intake not explicitly enabled")
    if config.get("enabled") is not True:
        raise ValueError("Linux Agent disabled")
    if not state.is_absolute() or state.is_symlink():
        raise ValueError("state directory must be absolute and not symlink")
    if state.exists():
        mode = state.stat().st_mode
        if not stat.S_ISDIR(mode) or mode & 0o077 or state.stat().st_uid != os.getuid():
            raise ValueError("state must be an owner-private directory (0700)")


@contextmanager
def daemon_lock(state: Path) -> Iterator[None]:
    state.mkdir(mode=0o700, parents=True, exist_ok=True)
    if state.is_symlink() or state.stat().st_mode & 0o077 or state.stat().st_uid != os.getuid():
        raise ValueError("unsafe state permissions")
    lock = state / "agent.lock"
    flags = os.O_RDWR | os.O_CREAT | os.O_NONBLOCK | os.O_NOFOLLOW
    fd = os.open(lock, flags, 0o600)
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_nlink != 1 or info.st_mode & 0o022):
            raise ValueError("unsafe daemon lock")
    except BaseException:
        os.close(fd)
        raise
    with os.fdopen(fd, "r+") as fp:
        fcntl.flock(fp.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield


def _result_envelope(task_id: str, config: dict[str, Any], digest: str,
                     result: dict[str, Any]) -> dict[str, Any]:
    return {"version": 1, "id": task_id, "status": result["status"],
            "target_host_id": config["host_id"],
            "target_installation_id": config["installation_id"],
            "task_digest": digest, "result": result}


class Agent:
    def __init__(self, config: dict[str, Any], state: Path):
        validate_config(config, state)
        self.config = config
        self.state = state
        self.transport = GitTransport(config, state)
        # This is the only installation-level stop signal; each task has its
        # own local watcher/cancellation state, never reused across tasks.
        self.stop_requested = threading.Event()
        # Project intake is NOT enabled by a registry alone. It requires the
        # separately selected private local service configuration flag.
        self.project_turn = None
        if "project_turn" in config:
            from linux_agent.repository.project_turn import ProjectTurn
            self.project_turn = ProjectTurn(config, state)

    def request_stop(self) -> None:
        self.stop_requested.set()

    def disabled(self) -> bool:
        # Every directory entry is a stop fence, including dangling symlinks.
        # Permission/I/O errors must fail closed rather than imply intake ON.
        try:
            (self.state / "EMERGENCY_STOP").lstat()
        except FileNotFoundError:
            return False
        return True

    def _claim_path(self, task_id: str) -> Path:
        return self.state / "claims" / (task_id + ".json")

    def _spool_path(self, task_id: str) -> Path:
        return self.state / "results" / (task_id + ".json")

    def _task_lease_dir(self) -> Path:
        return self.state / "locks" / "host-execution"

    def _task_lease_key(self, task_id: str) -> str:
        return f"host-task:{task_id}"

    def claim(self, task_id: str, digest: str) -> None:
        path = self._claim_path(task_id)
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        payload = {"version": 1, "id": task_id, "task_digest": digest,
                   "status": "execution_may_have_started"}
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(path, flags, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fp:
            fp.write(json.dumps(payload, sort_keys=True) + "\n")
            fp.flush()
            os.fsync(fp.fileno())
        fsync_directory(path.parent)

    def spool(self, envelope: dict[str, Any]) -> None:
        path = self._spool_path(envelope["id"])
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        # A dangling symlink also counts as an unsafe pre-existing spool.
        # Never replace it with a synthetic result during recovery.
        if path.is_symlink():
            raise ValueError("invalid durable result spool path")
        if path.exists():
            if json.loads(path.read_text()) != envelope:
                raise RuntimeError("immutable local result conflict")
            return
        atomic_write_text(path, json.dumps(envelope, sort_keys=True, ensure_ascii=False,
                                          separators=(",", ":")) + "\n")

    def recover(self) -> int:
        """Stale claims become UNKNOWN without replay, regardless of claim phase."""
        count = 0
        for claim in sorted((self.state / "claims").glob("*.json")):
            spool_path = self._spool_path(claim.stem)
            if spool_path.is_symlink():
                raise ValueError("invalid durable result spool path")
            if spool_path.exists():
                continue
            if claim.is_symlink() or not ID.fullmatch(claim.stem):
                raise ValueError("unsafe task claim path")
            # Only the exact fsynced pre-effect claim may authorize an
            # UNKNOWN result. Never manufacture one from ambiguous JSON,
            # changed status, extra fields or a truncated claim.
            try:
                claim_bytes = claim.read_bytes()
                if len(claim_bytes) > 4096:
                    raise ValueError("oversized claim")
                data = json.loads(claim_bytes, object_pairs_hook=_unique_pairs)
            except (OSError, UnicodeDecodeError, ValueError) as exc:
                raise ValueError("corrupt claim; fail closed") from exc
            if (not isinstance(data, dict)
                    or set(data) != {"version", "id", "task_digest", "status"}
                    or data["version"] != 1 or data["id"] != claim.stem
                    or data["status"] != "execution_may_have_started"
                    or not isinstance(data["task_digest"], str)
                    or not data["task_digest"]):
                raise ValueError("corrupt claim; fail closed")
            # The inherited task-specific Mac-style execution lease is the
            # precise ownership fence for descendants after SIGKILL.
            lease_path = execution_lease_path(
                self._task_lease_dir(), self._task_lease_key(claim.stem)
            )
            recover_task_lease(lease_path)
            result = {"version": 1, "id": claim.stem, "status": "unknown",
                      "reason": "interrupted_or_uncertain_execution_no_replay",
                      "target_host_id": self.config["host_id"],
                      "target_installation_id": self.config["installation_id"],
                      "task_digest": data["task_digest"]}
            self.spool(_result_envelope(claim.stem, self.config, data["task_digest"], result))
            count += 1
        return count

    def _verified_spool_bytes(self, path: Path) -> bytes:
        """Never publish altered/private stale bytes as an authoritative Git result.

        A completed result is immutable and must agree with its durable
        pre-effect claim and exact host/installation. Corruption fails closed;
        recovery must not rewrite a claimed execution or rerun its commands.
        """
        if path.is_symlink() or not path.is_file() or not ID.fullmatch(path.stem):
            raise ValueError("invalid durable result spool path")
        blob = path.read_bytes()
        if len(blob) > 4 * 1024 * 1024:
            raise ValueError("invalid durable result spool size")
        claim_path = self._claim_path(path.stem)
        if claim_path.is_symlink() or not claim_path.is_file():
            raise ValueError("invalid durable result: missing or unsafe claim")
        try:
            envelope = json.loads(blob, object_pairs_hook=_unique_pairs)
            claim = json.loads(claim_path.read_bytes(), object_pairs_hook=_unique_pairs)
        except (ValueError, UnicodeDecodeError) as exc:
            raise ValueError("invalid durable result: corrupt JSON") from exc
        if (not isinstance(envelope, dict) or not isinstance(claim, dict)
                or claim.get("version") != 1 or claim.get("id") != path.stem
                or claim.get("status") != "execution_may_have_started"
                or not isinstance(claim.get("task_digest"), str)
                or envelope.get("version") != 1
                or envelope.get("id") != path.stem
                or envelope.get("target_host_id") != self.config["host_id"]
                or envelope.get("target_installation_id") != self.config["installation_id"]
                or envelope.get("task_digest") != claim["task_digest"]
                or envelope.get("status") not in {"done", "failed", "unknown"}
                or not isinstance(envelope.get("result"), dict)
                or envelope["result"].get("id") != path.stem
                or envelope["result"].get("status") != envelope["status"]
                # Nested fields are optional in the Mac wire subset, but
                # when present cannot contradict the original durable claim.
                or envelope["result"].get("task_digest", claim["task_digest"])
                   != claim["task_digest"]
                or envelope["result"].get("target_host_id", self.config["host_id"])
                   != self.config["host_id"]
                or envelope["result"].get(
                    "target_installation_id", self.config["installation_id"]
                ) != self.config["installation_id"]):
            raise ValueError("invalid durable result: claim/envelope mismatch")
        return blob

    def publish_pending(self, *, remote_refreshed: bool = False) -> int:
        paths = sorted((self.state / "results").glob("*.json"))
        if not paths:
            return 0
        # Caller may share its freshly fetched snapshot with the inbox probe.
        # Every standalone publication remains independently refreshed.
        if not remote_refreshed:
            self.transport.refresh()
        count = 0
        for path in paths:
            data = self._verified_spool_bytes(path)
            existing = self.transport.remote_result(path.stem)
            if existing is None:
                # publish() independently refetches and compares to protect
                # against concurrent pushes, ambiguity and history rewrites.
                self.transport.publish(path.stem, data)
            elif existing != data:
                raise GitError("immutable remote result conflict")
            count += 1
        return count

    def _execute(self, task: dict[str, Any]) -> dict[str, Any]:
        cancel = threading.Event()
        interrupted = threading.Event()
        # Share the watcher fence with each command stage. Otherwise a STOP
        # observed between stages can still spawn the next shell after the
        # watcher has already returned.
        executor = RuntimeExecutor(Core(Path(task["cwd"])), interruption=interrupted)

        def watcher() -> None:
            while not cancel.wait(0.10):
                if self.disabled() or self.stop_requested.is_set():
                    interrupted.set()
                    executor.terminate_active_command()
                    return

        observer = threading.Thread(target=watcher, daemon=True)
        observer.start()
        try:
            # The stop file may appear after admission, before process spawn.
            if self.disabled() or self.stop_requested.is_set():
                interrupted.set()
                result = {"version": 1, "id": task["id"], "status": "unknown",
                          "reason": "emergency_stop_after_claim", "task_digest": task_digest(task)}
            else:
                result = executor.process_task(task)
        finally:
            cancel.set()
            observer.join(timeout=1)
        if interrupted.is_set() or self.disabled() or self.stop_requested.is_set():
            result["status"] = "unknown"
            result["reason"] = "emergency_stop_effects_uncertain"
        return result

    def once(self) -> dict[str, Any]:
        with daemon_lock(self.state):
            return self._once_locked()

    @staticmethod
    def adaptive_delay(poll_seconds: float, last_activity_at: float | None,
                       now: float) -> float:
        """2s hot, 5s warm, bounded idle; never faster than explicit cap."""
        if last_activity_at is None:
            return poll_seconds
        age = max(0.0, now - last_activity_at)
        return min(poll_seconds, 2.0 if age < 30 else
                   5.0 if age < 120 else poll_seconds)

    def serve(self, poll_seconds: float = 15.0, *, adaptive: bool = False) -> None:
        """One serial scheduler under a lifetime daemon flock; never auto-start."""
        if not 0.2 <= poll_seconds <= 3600:
            raise ValueError("invalid daemon poll interval")
        with daemon_lock(self.state):
            # Git outages are not fatal scheduler failures. Completed results
            # stay in the durable spool; the next successful poll republishes
            # the original bytes without ever re-executing a claimed task.
            # Corrupt claims, invalid state and unexpected errors still fail
            # closed and let the service manager detect the failure.
            git_unavailable = False
            last_activity_at: float | None = None
            while not self.stop_requested.is_set():
                published_task = False
                try:
                    outcome = self._once_locked()
                    if outcome["status"] not in {"idle", "disabled", "stopping"}:
                        last_activity_at = time.monotonic()
                    # A completed task has already paid the full Git
                    # verification/push cost. Drain a queued next task
                    # immediately rather than sleeping another hot interval.
                    # Busy resources and Git outages still use normal delay.
                    published_task = outcome["status"] in {"done", "failed", "unknown"}
                    git_unavailable = False
                except GitError:
                    if not git_unavailable:
                        import sys
                        print("linux-agent: Git unavailable; retrying durable "
                              "publication/intake without replay",
                              file=sys.stderr, flush=True)
                    git_unavailable = True
                delay = (0.0 if published_task else
                         self.adaptive_delay(poll_seconds, last_activity_at,
                                             time.monotonic()) if adaptive
                         else poll_seconds)
                self.stop_requested.wait(delay)

    def _once_locked(self) -> dict[str, Any]:
        # Recovery and publication remain possible while intake is disabled.
        self.recover()
        # One fresh snapshot covers both durable publication and inbox reads.
        # publish() still refetches independently before every actual push.
        try:
            self.transport.refresh()
            self.publish_pending(remote_refreshed=True)
        except GitError:
            # Host and project are independent Git mailboxes. An opted-in
            # project's turn may proceed despite a host Git outage; host
            # durable results remain in their spool for later retry.
            # The default host-only installation still raises unchanged.
            if self.project_turn is None:
                raise
            if self.stop_requested.is_set():
                return {"status": "stopping"}
            if self.disabled():
                return {"status": "disabled"}
            return self.project_turn.once_locked(self)
        if self.stop_requested.is_set():
            return {"status": "stopping"}
        if self.disabled():
            return {"status": "disabled"}
        claimed = {path.stem for path in (self.state / "claims").glob("*.json")}
        for task_id, raw in self.transport.task_candidates(skip_ids=claimed):
            if self._claim_path(task_id).exists():
                continue
            try:
                task = json.loads(raw, object_pairs_hook=_unique_pairs)
            except (ValueError, UnicodeDecodeError):
                continue  # Unparseable payload is not evidence of a target match.
            if not isinstance(task, dict):
                continue
            if task.get("target_host_id") != self.config["host_id"] or task.get("target_installation_id") != self.config["installation_id"]:
                continue
            if task.get("id") != task_id:
                continue
            digest = task_digest(task)
            try:
                validate_linux_host_subset(task)
                validate_task(task)
            except ValueError as exc:
                result = {"version": 1, "id": task_id, "status": "failed",
                          "reason": "invalid_task_file", "error": str(exc)[:500],
                          "task_digest": digest}
                self.claim(task_id, digest)
                self.spool(_result_envelope(task_id, self.config, digest, result))
                # The intake snapshot is already fresh; publish() re-fetches
                # immediately before push and independently confirms remote.
                self.publish_pending(remote_refreshed=True)
                return {"status": "failed", "id": task_id, "reason": "invalid_task_file"}
            if self.stop_requested.is_set():
                return {"status": "stopping"}
            if self.disabled():
                return {"status": "disabled"}
            # Re-fetch and check same immutable task before writing claim.
            self.transport.refresh()
            stable = self.transport.pending_task(task_id)
            if stable != raw:
                return {"status": "task_changed_before_claim", "id": task_id}
            try:
                # The established Mac lock arbitration protects named
                # resources and "machine" exclusivity. Busy != claimed.
                with machine_resource_lease(
                    task, lock_dir=resource_lock_dir(self.state),
                    command_env=os.environ,
                ):
                    if self.disabled():
                        return {"status": "disabled"}
                    # Task lease descriptors survive fork/exec. If the Python
                    # supervisor is SIGKILLed, future recovery can identify
                    # and fence only its own orphan command descendants.
                    with closing(acquire_execution_leases(
                        self._task_lease_dir(), (self._task_lease_key(task_id),)
                    )) as execution:
                        previous = os.environ.get("LOCAL_AGENT_LEASE_FDS")
                        previous_digest = os.environ.get("LOCAL_AGENT_LEASE_KEYS_DIGEST")
                        os.environ.update(execution.environment())
                        try:
                            self.claim(task_id, digest)  # fsync before first child spawn
                            try:
                                result = self._execute(task)
                            except BaseException as exc:
                                result = {"version": 1, "id": task_id, "status": "unknown",
                                          "reason": "execution_interrupted_or_uncertain",
                                          "error_type": type(exc).__name__, "task_digest": digest}
                            self.spool(_result_envelope(task_id, self.config, digest, result))
                        finally:
                            for key, value in (
                                ("LOCAL_AGENT_LEASE_FDS", previous),
                                ("LOCAL_AGENT_LEASE_KEYS_DIGEST", previous_digest),
                            ):
                                if value is None:
                                    os.environ.pop(key, None)
                                else:
                                    os.environ[key] = value
            except MachineResourceBusy as exc:
                return {"status": "resource_busy", "resource": exc.resource, "id": task_id}
            # Reuse the pre-claim refresh; the publication itself still
            # independently fetches, detects conflicts and verifies push.
            self.publish_pending(remote_refreshed=True)
            return {"status": result["status"], "id": task_id, "digest": digest}
        if self.project_turn is not None:
            return self.project_turn.once_locked(self)
        return {"status": "idle"}


def config_from_file(path: Path) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file() or path.stat().st_mode & 0o077:
        raise ValueError("config must be regular, owner-private (mode 0600)")
    return json.loads(path.read_text(encoding="utf-8"))
