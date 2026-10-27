"""Locally authorized, repo-free Host Task Executor (explicit opt-in CLI).

The queue is owned by the logged-in operator, never by the writable Git
control branch. A Git task/transport is NOT an execution grant. Each request
is tied to one host installation and consumed at most once. Interrupted
claims are terminal UNKNOWN and never retried.
"""
from __future__ import annotations

import argparse
import contextlib
import fcntl
import hashlib
import json
import os
import re
import stat
import threading
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import local_agent.foundation.core as core
import local_agent.operator.local as operator_local
from local_agent.foundation.process import atomic_write_text, fsync_directory
from local_agent.repository.host_facts import load_local_host_identity
from local_agent.repository.host_routing import HostIdentity
from local_agent.runtime.executor import RuntimeExecutor
from local_agent.runtime.task_contract import (
    task_dedupe_identity, task_digest, task_resources_for, validate_task,
)
from local_agent.supervisor.resources import machine_resource_lease, resource_lock_dir
from local_agent.supervisor.macos_release_admission import (
    MacReleaseAdmissionBlocked, macos_task_admission,
)

HOST_TASK_VERSION = 1
MAX_LOCAL_REQUEST_BYTES = 4 * 1024 * 1024
_TASK_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,120}$")
_DISALLOWED = frozenset({
    "repository", "repository_id", "target_repository_id", "agent_binding",
    "work_branch", "patch", "writes", "deletes", "payload_file", "allow_write",
})
_ALLOWED = frozenset({
    "version", "id", "target_host_id", "target_installation_id", "cwd",
    "commands", "verify_commands", "steps", "verify_steps", "resources",
    "task_timeout", "command_timeout", "idle_timeout", "memory_limit_mb",
    "dedupe_key", "dedupe_revision", "output_policy", "verification_level",
    "verification_policy", "mode",
})


def _unique_pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in items:
        if key in result:
            raise ValueError(f"duplicate host task field: {key}")
        result[key] = value
    return result


def _require_private_dir(path: Path, *, create: bool = False) -> None:
    if create:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_mode & 0o077:
        raise ValueError("host task state directory must be private and not symlinked")
    if os.name == "posix" and info.st_uid != os.getuid():
        raise ValueError("host task state directory owner differs from current OS user")


def _load_private_request(path: Path) -> dict[str, Any]:
    if path.is_symlink():
        raise ValueError("host task request must not be a symlink")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077:
            raise ValueError("host task request must be a private regular file")
        if os.name == "posix" and info.st_uid != os.getuid():
            raise ValueError("host task request has a different owner")
        if not 0 < info.st_size <= MAX_LOCAL_REQUEST_BYTES:
            raise ValueError("host task request exceeds the byte limit")
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            raw = stream.read(MAX_LOCAL_REQUEST_BYTES + 1)
        if len(raw) > MAX_LOCAL_REQUEST_BYTES:
            raise ValueError("host task request exceeds the byte limit")
        task = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_pairs)
    finally:
        os.close(descriptor)
    if not isinstance(task, dict):
        raise ValueError("host task request must be an object")
    return task


def validate_local_host_task(task: dict[str, Any], source: HostIdentity) -> tuple[dict[str, Any], Path]:
    if not isinstance(task, dict) or set(task) - _ALLOWED or set(task) & _DISALLOWED:
        raise ValueError("host task contains unsupported or project-specific fields")
    if type(task.get("version")) is not int or task["version"] != HOST_TASK_VERSION:
        raise ValueError("unsupported host task version")
    task_id = task.get("id")
    if not isinstance(task_id, str) or _TASK_ID_RE.fullmatch(task_id) is None:
        raise ValueError("host task id must be a bounded path-safe component")
    if (task.get("target_host_id") != source.host_id or
            task.get("target_installation_id") != source.installation_id):
        raise ValueError("host task targets a different installation")
    cwd = task.get("cwd")
    if not isinstance(cwd, str) or not cwd or not Path(cwd).is_absolute():
        raise ValueError("host task needs an absolute working directory")
    workspace = Path(cwd)
    if workspace.is_symlink() or not workspace.is_dir():
        raise ValueError("host task cwd must be an existing directory")
    # Do not force cwd to be a Git project; legitimate local directories are valid.
    if not (task.get("commands") or task.get("steps")):
        raise ValueError("host task needs at least one command or step")
    # Reuse the existing immutable task, resources and timeout contracts. No
    # fake repository binding or Git branch is attached to this payload.
    normalized = {key: value for key, value in task.items()
                  if key not in ("version", "target_host_id", "target_installation_id", "cwd")}
    normalized.setdefault("resources", [])
    validate_task(normalized, allow_local_codex=True)
    task_resources_for(normalized)
    return normalized, workspace


@dataclass(slots=True)
class _HostCore:
    WORK: Path
    ENV: dict[str, str]

    now_iso = staticmethod(core.now_iso)
    log = staticmethod(core.log)
    command_log_descriptor = staticmethod(core.command_log_descriptor)
    stage_plan_for = staticmethod(core.stage_plan_for)

    def __getattr__(self, name: str) -> Any:
        # RuntimeExecutor expects additional stable core constants and helpers.
        # Delegate those read-only APIs, but keep WORK/ENV and the repository-
        # free process_task implementation isolated to this task instance.
        if name.startswith("_"):
            raise AttributeError(name)
        return getattr(core, name)

    def process_task(self, task: dict[str, Any], *, command_runner: Any) -> dict[str, Any]:
        # No git prepare_work, checkout, patch, cleanup, worktree reset or
        # repository result publication. This is a normal OS command lane.
        plan = core.stage_plan_for(task)
        command_timeout = core.command_timeout_for(task)
        result: dict[str, Any] = {
            "id": task["id"], "status": "done", "started_at": core.now_iso(),
            "commands": [], "verification": [], "stages": [],
        }
        for stage in plan:
            timeout = int(stage.get("stage_timeout") or command_timeout)
            outcome = command_runner(stage["command"], timeout, stage=stage)
            phase = "commands" if stage["stage_phase"] == "commands" else "verification"
            result[phase].append(outcome)
            result["stages"].append({
                "stage_name": stage["stage_name"],
                "stage_phase": stage["stage_phase"],
                "exit_code": outcome.get("exit_code"),
                "elapsed_seconds": outcome.get("elapsed_seconds"),
            })
            if outcome.get("exit_code") != 0:
                result.update(
                    status="failed",
                    failure_reason=("command_failed" if phase == "commands" else "verification_failed"),
                )
                break
        result["finished_at"] = core.now_iso()
        return result


def _durable_exclusive_claim(path: Path, payload: dict[str, Any]) -> bool:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n"
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return False
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    fsync_directory(path.parent)
    return True


def _durable_private_result(path: Path, result: dict[str, Any]) -> None:
    data = json.dumps(result, sort_keys=True, ensure_ascii=False, indent=2) + "\n"
    # Keep the existing durable atomic write/fsync mechanism, with private
    # directory ownership as the operator authorization boundary.
    atomic_write_text(path, data)
    os.chmod(path, 0o600)


@contextlib.contextmanager
def _host_task_turn(base: Path) -> Iterator[None]:
    """Serialize this machine's local task ledger, not unrelated project work."""
    _require_private_dir(base, create=True)
    path = base / "runner.lock"
    if path.is_symlink():
        raise ValueError("host task runner lock must not be a symlink")
    handle = os.open(path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        if os.fstat(handle).st_mode & 0o077:
            raise ValueError("host task runner lock is not private")
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("host task runner currently active") from exc
        yield
    finally:
        os.close(handle)


def recover_interrupted_local_tasks(state_dir: Path) -> int:
    """Recover only when no live task runner owns the machine's host ledger."""
    base = Path(state_dir) / "host-tasks"
    if not base.exists():
        return 0
    with _host_task_turn(base):
        return _recover_interrupted_local_tasks_unlocked(state_dir)


def _recover_interrupted_local_tasks_unlocked(state_dir: Path) -> int:
    """Seal interrupted claims as UNKNOWN; never reexecute their commands."""
    base = Path(state_dir) / "host-tasks"
    claims, results = base / "claims", base / "results"
    if not claims.exists():
        return 0
    _require_private_dir(base)
    _require_private_dir(claims)
    _require_private_dir(results, create=True)
    count = 0
    for claim in sorted(claims.glob("*.json")):
        if claim.is_symlink() or not _TASK_ID_RE.fullmatch(claim.stem):
            raise ValueError("unsafe host task claim filename")
        output = results / claim.name
        if output.exists():
            continue
        entry = _load_private_request(claim)
        if entry.get("id") != claim.stem or not isinstance(entry.get("task_digest"), str):
            raise ValueError("invalid host task claim record")
        _durable_private_result(output, {
            "id": claim.stem, "task_digest": entry["task_digest"],
            "status": "unknown", "failure_reason": "interrupted_execution",
            "replayed": False, "finished_at": core.now_iso(),
        })
        count += 1
    return count


def record_rejected_host_task(
    task: dict[str, Any], *, state_dir: Path, identity: HostIdentity, error: str,
) -> dict[str, Any]:
    """Persist a pre-claim rejection under the execution ledger lock."""
    task_id = task.get("id")
    if not isinstance(task_id, str) or _TASK_ID_RE.fullmatch(task_id) is None:
        raise ValueError("invalid rejected host task ID")
    if (task.get("target_host_id") != identity.host_id or
            task.get("target_installation_id") != identity.installation_id):
        raise ValueError("rejected host task targets a different installation")
    base = Path(state_dir) / "host-tasks"
    with _host_task_turn(base):
        if operator_local.is_disabled():
            raise ValueError("operator disabled before host task rejection")
        results = base / "results"
        _require_private_dir(results, create=True)
        result_path = results / f"{task_id}.json"
        if result_path.exists():
            return _load_private_request(result_path)
        if (base / "claims" / f"{task_id}.json").exists():
            raise ValueError("cannot reject an already claimed host task")
        result = {
            "version": HOST_TASK_VERSION, "id": task_id,
            "target_host_id": identity.host_id,
            "target_installation_id": identity.installation_id,
            "task_digest": task_digest(task), "status": "failed",
            "failure_reason": "invalid_task_file", "error": error[:2000],
            "started_at": None, "finished_at": core.now_iso(),
            "commands": [], "verification": [], "stages": [], "replayed": False,
        }
        _durable_private_result(result_path, result)
        return result


def run_local_host_task(
    task_id: str, *, state_dir: Path, identity_file: Path,
) -> dict[str, Any] | None:
    """Run an explicitly local OS-user task; no Git, catalog or projects needed.

    This direct invocation is trusted only as a local operator action. It is
    NOT a remote task or an authorization grant for the Git control checkout.
    """
    with macos_task_admission(state_dir):
        return _run_admitted_local_host_task(
            task_id, state_dir=state_dir, identity_file=identity_file,
        )


def _run_admitted_local_host_task(
    task_id: str, *, state_dir: Path, identity_file: Path,
) -> dict[str, Any] | None:
    base = Path(state_dir) / "host-tasks"
    with _host_task_turn(base):
        return _run_local_host_task_unlocked(
            task_id, state_dir=state_dir, identity_file=identity_file,
        )


def _run_local_host_task_unlocked(
    task_id: str, *, state_dir: Path, identity_file: Path,
) -> dict[str, Any] | None:
    """Execute a locally operator-owned request with exact installation target.

    There is deliberately no dependency on a Git transport/checkout here.
    Remote operator enrollment is a separate, not-yet-enabled intake lane.
    """
    if _TASK_ID_RE.fullmatch(task_id) is None:
        raise ValueError("invalid local host task id")
    source = load_local_host_identity(identity_file)
    base = Path(state_dir) / "host-tasks"
    _require_private_dir(base, create=True)
    requests, claims, results = (base / name for name in ("requests", "claims", "results"))
    for directory in (requests, claims, results):
        _require_private_dir(directory, create=True)
    if operator_local.is_disabled():
        raise ValueError("Local Agent has been disabled by the operator")
    req_path = requests / f"{task_id}.json"
    task = _load_private_request(req_path)
    normalized, workspace = validate_local_host_task(task, source)
    if normalized["id"] != task_id:
        raise ValueError("host task filename and ID disagree")
    digest = task_digest(task)
    result_path = results / f"{task_id}.json"
    if result_path.exists():
        return None
    claim = claims / f"{task_id}.json"
    if claim.exists():
        return None
    adapter = _HostCore(workspace, dict(core.ENV))
    # Acquire resources before claiming: a busy USB port or whole-host
    # exclusion is ordinary deferral, NOT a consumed/unknown task.
    with machine_resource_lease(
        normalized, lock_dir=resource_lock_dir(state_dir),
        command_env=adapter.ENV,
    ):
        if operator_local.is_disabled():
            raise ValueError("operator disabled before host task admission")
        key, revision = task_dedupe_identity(normalized)
        if key is not None:
            dedupe_dir = base / "dedupe"
            _require_private_dir(dedupe_dir, create=True)
            dedupe_hash = hashlib.sha256(
                f"{key}\\0{revision}".encode("utf-8"),
            ).hexdigest()
            if not _durable_exclusive_claim(dedupe_dir / f"{dedupe_hash}.json", {
                "id": task_id, "task_digest": digest,
                "dedupe_key": key, "dedupe_revision": revision,
            }):
                raise ValueError("host task dedupe revision was already admitted")
        if not _durable_exclusive_claim(claim, {
            "id": task_id, "task_digest": digest,
            "target_host_id": source.host_id,
            "target_installation_id": source.installation_id,
            "claimed_at": core.now_iso(), "status": "claimed",
        }):
            return None
        # Claim exists BEFORE any effects. Interrupted executions remain UNKNOWN.
        stop = threading.Event()
        interrupted = threading.Event()
        # This is an explicit local OS-user action, not remotely planned
        # repository work. Ordinary tools such as Codex are permitted here.
        executor = RuntimeExecutor(
            adapter, task_validator=lambda candidate: validate_task(
                candidate, allow_local_codex=True,
            ),
        )

        def watch_emergency_stop() -> None:
            while not stop.wait(0.25):
                if operator_local.is_disabled():
                    interrupted.set()
                    executor.terminate_active_command()
                    return

        watcher = threading.Thread(
            target=watch_emergency_stop, name="host-task-disable-watch", daemon=True,
        )
        watcher.start()
        try:
            if operator_local.is_disabled():
                raise RuntimeError("operator disabled after host task claim")
            outcome = executor.process_task(normalized)
            if interrupted.is_set():
                outcome.update(
                    status="failed", failure_reason="operator_disabled",
                )
            outcome.update(
                version=HOST_TASK_VERSION, target_host_id=source.host_id,
                target_installation_id=source.installation_id,
                task_digest=digest,
            )
        except Exception as exc:
            outcome = {
                "id": task_id, "version": HOST_TASK_VERSION,
                "target_host_id": source.host_id,
                "target_installation_id": source.installation_id,
                "task_digest": digest, "status": "unknown",
                "failure_reason": "execution_outcome_unverified",
                "error": f"{type(exc).__name__}: {exc}"[:2000],
                "finished_at": core.now_iso(), "replayed": False,
            }
        finally:
            stop.set()
            watcher.join(timeout=2)
        _durable_private_result(result_path, outcome)
    return outcome

def main() -> int:
    parser = argparse.ArgumentParser(description="Explicit local standalone host task runner")
    parser.add_argument("--id", required=True, help="operator-owned local host task id")
    parser.add_argument("--state-dir", type=Path,
                        default=Path.home() / "Library" / "Application Support" / "local-agent")
    parser.add_argument("--identity-file", type=Path, required=True,
                        help="private local host identity created during host initialization")
    parser.add_argument("--execute", action="store_true",
                        help="run one local operator-owned task; otherwise no effects")
    options = parser.parse_args()
    if not options.execute:
        parser.error("explicit --execute is required; no task has been claimed")
    try:
        outcome = run_local_host_task(
            options.id, state_dir=options.state_dir,
            identity_file=options.identity_file,
        )
    except MacReleaseAdmissionBlocked as exc:
        print(f"Mac release task admission deferred: {exc}", flush=True)
        return 2
    except (ValueError, OSError) as exc:
        parser.error(str(exc))
    if outcome is None:
        print("host task already processed or claimed; no replay", flush=True)
        return 2
    print(json.dumps(outcome, ensure_ascii=False, sort_keys=True), flush=True)
    return 0 if outcome["status"] == "done" else 1


if __name__ == "__main__":
    raise SystemExit(main())
