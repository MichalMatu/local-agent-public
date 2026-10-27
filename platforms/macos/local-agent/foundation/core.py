#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Protocol

import local_agent.foundation.storage as storage
from local_agent.config import TIMEOUTS
from local_agent.foundation.process import (
    BoundedTextBuffer,
    atomic_write_text,
    fsync_directory,
    popen_registered,
    run_argv_bounded,
    spawn_shell,
    start_output_pump,
    terminate_remaining_process_group,
    terminate_process_group,
    unregister_process,
)

HOME = Path.home()
CONTROL = HOME / "agent-workspace" / "control"
WORK = HOME / "agent-workspace" / "work"
CHECKPOINTS = HOME / "agent-workspace" / "checkpoints"
CONTROL_BRANCH = "agent-control"
RESULTS_DIR = ".agent/results"
WORK_PREPARATION_MODE = "managed"  # changed only inside isolated repository workers

POLL_SECONDS = 15
COMMAND_TIMEOUT = TIMEOUTS.command_default
MAX_COMMAND_TIMEOUT = TIMEOUTS.command_max
MAX_OUTPUT = 60000
CHECKPOINT_TIMEOUT_SECONDS = 600
CHECKPOINT_MAX_FILES = 10_000
CHECKPOINT_MAX_BYTES = 5 * 1024**3
CHECKPOINT_MAX_PATCH_BYTES = 1024**3
CHECKPOINT_GIT_OUTPUT_BYTES = 16 * 1024**2
CHECKPOINT_FREE_SPACE_RESERVE_BYTES = 256 * 1024**2
CHECKPOINT_COPY_CHUNK_BYTES = 1024 * 1024
EFFICIENT_VERIFICATION_POLICY = "efficient-verification-v1"
VERIFICATION_LEVELS = frozenset({"work", "focused", "full"})
OUTPUT_POLICIES = frozenset({"stream", "summary"})
VERBOSE_COMMAND_LOG_ENV = "LOCAL_AGENT_VERBOSE_LOGS"
COMMAND_LOG_PREVIEW_CHARS = 160

BASE_PATH = [
    str(HOME / ".platformio" / "penv" / "bin"),
    "/opt/homebrew/bin",
    "/usr/local/bin",
    "/usr/bin",
    "/bin",
    "/usr/sbin",
    "/sbin",
]
ENV = os.environ.copy()
ENV["PATH"] = ":".join(BASE_PATH + [ENV.get("PATH", "")])

_BRANCH_RE = re.compile(r"^[A-Za-z0-9._/-]+$")
CONTROL_GIT_LOCK = threading.RLock()


class CommandRunner(Protocol):
    def __call__(
        self,
        command: str,
        timeout: int,
        *,
        stage: dict[str, Any] | None = None,
    ) -> dict[str, Any]: ...


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def log(message: str) -> None:
    print(f"[{now_iso()}] {message}", flush=True)


def verbose_command_logging() -> bool:
    return os.environ.get(VERBOSE_COMMAND_LOG_ENV, "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def command_log_descriptor(
    command: str,
    stage: dict[str, Any] | None = None,
) -> str:
    if verbose_command_logging():
        return command
    stage_name = str((stage or {}).get("name", "")).strip()
    line_count = max(1, len(command.splitlines()))
    if stage_name:
        if line_count > 1 or len(command) > COMMAND_LOG_PREVIEW_CHARS:
            return f"stage={stage_name} lines={line_count} chars={len(command)}"
        return f"stage={stage_name}"
    compact = " ".join(command.split())
    if line_count > 1 or len(compact) > COMMAND_LOG_PREVIEW_CHARS:
        return f"shell lines={line_count} chars={len(command)}"
    return compact


def bounded(text: str, limit: int = MAX_OUTPUT) -> str:
    if len(text) <= limit:
        return text
    return f"[... truncated {len(text) - limit} chars ...]\n{text[-limit:]}"


def process(
    args: list[str],
    cwd: Path,
    timeout: int = 120,
    *,
    input_text: str | None = None,
    output_limit: int = MAX_OUTPUT,
    environment: Mapping[str, str] | None = None,
    log_commands: bool = True,
) -> dict[str, Any]:
    command_text = " ".join(args)
    quiet_internal_git = bool(
        log_commands
        and args
        and args[0] == "git"
        and not verbose_command_logging()
    )
    process_log = log if log_commands and not quiet_internal_git else lambda _message: None
    if log_commands and not quiet_internal_git:
        log(f"exec: {command_text}")
    result = run_argv_bounded(
        args,
        cwd=cwd,
        env=ENV if environment is None else environment,
        timeout=timeout,
        output_limit=output_limit,
        log=process_log,
        input_text=input_text,
    )
    elapsed = float(result["elapsed_seconds"])
    if result.get("timed_out"):
        log(f"TIMEOUT after {timeout}s: {command_text}")
    if log_commands and (
        not quiet_internal_git
        or result["exit_code"] != 0
        or result.get("timed_out")
    ):
        log(
            f"exec finished exit={result['exit_code']} "
            f"elapsed={elapsed:.1f}s: {command_text}"
        )
    return result


def kill_process_group(proc: subprocess.Popen[str]) -> None:
    terminate_process_group(proc, log)


def run_command(
    command: str,
    timeout: int,
    *,
    stage: dict[str, Any] | None = None,
) -> dict[str, Any]:
    command_descriptor = command_log_descriptor(command, stage)
    log(f"exec: {command_descriptor}")
    started = time.monotonic()
    proc = spawn_shell(command, cwd=WORK, env=ENV)
    pump = start_output_pump(proc)
    output = BoundedTextBuffer(MAX_OUTPUT)
    reader_done = False
    timed_out = False
    background_process_leak = False
    direct_process_finished = False

    try:
        while True:
            elapsed = time.monotonic() - started
            remaining = timeout - elapsed
            if remaining <= 0 and proc.poll() is None and not timed_out:
                timed_out = True
                log(f"TIMEOUT after {timeout}s: {command_descriptor}")
                kill_process_group(proc)

            if proc.poll() is not None and not direct_process_finished:
                direct_process_finished = True
                background_process_leak = terminate_remaining_process_group(proc, log)

            try:
                item = pump.queue.get(timeout=0.25 if remaining > 0 else 0.05)
                if item is None:
                    reader_done = True
                else:
                    print(f"[CMD] {item}", end="", flush=True)
                    output.append(item)
            except queue.Empty:
                pass

            if direct_process_finished and reader_done:
                break
    finally:
        if proc.poll() is None:
            kill_process_group(proc)
        pump.stop()
        unregister_process(proc)

    exit_code = proc.returncode if proc.returncode is not None else 124
    if timed_out:
        exit_code = 124
    elif background_process_leak:
        exit_code = 126

    elapsed = time.monotonic() - started
    log(f"exec finished exit={exit_code} elapsed={elapsed:.1f}s: {command_descriptor}")

    return {
        "command": command,
        "exit_code": exit_code,
        "output": output.text(),
        "elapsed_seconds": round(elapsed, 3),
        "timed_out": timed_out,
        "background_process_leak": background_process_leak,
        **(
            {"failure_reason": "background_process_leak"}
            if background_process_leak
            else {}
        ),
    }


def validate_branch(branch: str, *, cwd: Path | None = None) -> str:
    if (
        not branch
        or not _BRANCH_RE.fullmatch(branch)
        or ".." in branch
        or branch.startswith("/")
        or branch.endswith("/")
    ):
        raise ValueError(f"invalid work_branch: {branch!r}")

    check = process(["git", "check-ref-format", "--branch", branch], cwd or CONTROL, timeout=30)
    if check["exit_code"] != 0:
        raise ValueError(f"invalid work_branch: {branch!r}")
    return branch


def sync_control() -> None:
    storage.sync_control(sys.modules[__name__])


def _run_git_capture(args: list[str], *, timeout: float = 60) -> dict[str, Any]:
    return process(
        ["git", *args],
        cwd=WORK,
        timeout=int(max(1, timeout)),
        output_limit=CHECKPOINT_GIT_OUTPUT_BYTES,
        log_commands=False,
    )


def _checkpoint_component(value: str) -> str:
    clean = re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip("-.")
    return clean[:160] or "workspace"


def _checkpoint_remaining(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError(
            f"workspace checkpoint exceeded {CHECKPOINT_TIMEOUT_SECONDS} seconds"
        )
    return remaining


def _require_complete_git_output(result: dict[str, Any], operation: str) -> str:
    if result["exit_code"] != 0:
        raise RuntimeError(f"{operation} failed: {result.get('output', '')}")
    if result.get("output_truncated"):
        raise RuntimeError(
            f"{operation} output exceeds {CHECKPOINT_GIT_OUTPUT_BYTES} characters"
        )
    return str(result.get("output", ""))


def _copy_checkpoint_file(source: Path, target: Path, deadline: float) -> int:
    copied = 0
    target.parent.mkdir(parents=True, exist_ok=True)
    with source.open("rb") as source_handle, target.open("xb") as target_handle:
        while True:
            _checkpoint_remaining(deadline)
            chunk = source_handle.read(CHECKPOINT_COPY_CHUNK_BYTES)
            if not chunk:
                break
            copied += len(chunk)
            if copied > CHECKPOINT_MAX_BYTES:
                raise RuntimeError(
                    f"workspace checkpoint exceeds {CHECKPOINT_MAX_BYTES} bytes"
                )
            target_handle.write(chunk)
        target_handle.flush()
        os.fsync(target_handle.fileno())
    shutil.copystat(source, target, follow_symlinks=False)
    return copied


def _fsync_checkpoint_tree(root: Path) -> None:
    directories = [path for path in root.rglob("*") if path.is_dir()]
    for directory in sorted(directories, key=lambda path: len(path.parts), reverse=True):
        fsync_directory(directory)
    fsync_directory(root)


def _write_tracked_patch(path: Path, deadline: float, reserved_bytes: int) -> int:
    with path.open("xb") as patch_file:
        proc = popen_registered(
            ["git", "diff", "--binary", "--full-index", "HEAD", "--"],
            cwd=WORK,
            env=ENV,
            stdout=patch_file,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        setattr(proc, "_local_agent_process_group", proc.pid)
        try:
            while proc.poll() is None:
                _checkpoint_remaining(deadline)
                patch_size = patch_file.tell()
                if patch_size > CHECKPOINT_MAX_PATCH_BYTES:
                    raise RuntimeError(
                        f"tracked checkpoint patch exceeds "
                        f"{CHECKPOINT_MAX_PATCH_BYTES} bytes"
                    )
                free = shutil.disk_usage(path.parent).free
                if free < reserved_bytes + CHECKPOINT_FREE_SPACE_RESERVE_BYTES:
                    raise RuntimeError(
                        "insufficient free space while creating tracked patch"
                    )
                time.sleep(0.05)
            stderr = (
                proc.stderr.read(CHECKPOINT_GIT_OUTPUT_BYTES) if proc.stderr else b""
            )
            if proc.returncode != 0:
                raise RuntimeError(stderr.decode("utf-8", errors="replace"))
            patch_file.flush()
            os.fsync(patch_file.fileno())
            return patch_file.tell()
        finally:
            if proc.poll() is None:
                terminate_process_group(proc, log)
            if proc.stderr is not None:
                proc.stderr.close()
            unregister_process(proc)


def checkpoint_worktree(task_id: str, *, reason: str) -> dict[str, Any] | None:
    """Persist recoverable dirty workspace state before any destructive cleanup."""
    deadline = time.monotonic() + CHECKPOINT_TIMEOUT_SECONDS
    status_text = _require_complete_git_output(
        _run_git_capture(
            ["status", "--porcelain=v1", "-z"],
            timeout=_checkpoint_remaining(deadline),
        ),
        "git status",
    )
    if not status_text:
        return None

    base_head = _require_complete_git_output(
        _run_git_capture(["rev-parse", "HEAD"], timeout=_checkpoint_remaining(deadline)),
        "git rev-parse",
    ).strip()
    branch_result = _run_git_capture(
        ["branch", "--show-current"],
        timeout=_checkpoint_remaining(deadline),
    )
    branch = str(branch_result.get("output", "")).strip() if branch_result["exit_code"] == 0 else ""

    untracked_text = _require_complete_git_output(
        _run_git_capture(
            ["ls-files", "--others", "--exclude-standard", "-z"],
            timeout=_checkpoint_remaining(deadline),
        ),
        "git ls-files",
    )
    raw_untracked = [item for item in untracked_text.split("\0") if item]
    if len(raw_untracked) > CHECKPOINT_MAX_FILES:
        raise RuntimeError(
            f"workspace checkpoint has {len(raw_untracked)} untracked files; "
            f"limit is {CHECKPOINT_MAX_FILES}"
        )

    entries: list[tuple[str, Path, int]] = []
    untracked_bytes = 0
    for relative_text in raw_untracked:
        _checkpoint_remaining(deadline)
        relative = Path(relative_text)
        if relative.is_absolute() or ".." in relative.parts:
            raise RuntimeError(f"unsafe untracked path from git: {relative_text!r}")
        source = WORK / relative
        if source.is_symlink():
            size = len(os.fsencode(os.readlink(source)))
        elif source.is_file():
            size = source.stat().st_size
        else:
            raise RuntimeError(f"unsupported untracked entry: {relative_text!r}")
        untracked_bytes += size
        if untracked_bytes > CHECKPOINT_MAX_BYTES:
            raise RuntimeError(
                f"workspace checkpoint exceeds {CHECKPOINT_MAX_BYTES} bytes"
            )
        entries.append((relative_text, source, size))

    task_component = _checkpoint_component(task_id)
    reason_component = _checkpoint_component(reason)
    checkpoint_root = CHECKPOINTS / task_component
    checkpoint_root.mkdir(parents=True, exist_ok=True)
    fsync_directory(checkpoint_root.parent)
    required_free = untracked_bytes + CHECKPOINT_FREE_SPACE_RESERVE_BYTES
    if shutil.disk_usage(checkpoint_root).free < required_free:
        raise RuntimeError(
            f"insufficient free space for workspace checkpoint; required={required_free}"
        )
    final_dir = checkpoint_root / f"{time.time_ns()}-{reason_component}"
    temp_dir = final_dir.with_name(final_dir.name + ".tmp")
    temp_dir.mkdir(parents=True, exist_ok=False)

    try:
        patch_path = temp_dir / "tracked.patch"
        patch_bytes = _write_tracked_patch(patch_path, deadline, untracked_bytes)
        if patch_bytes + untracked_bytes > CHECKPOINT_MAX_BYTES:
            raise RuntimeError(
                f"workspace checkpoint exceeds {CHECKPOINT_MAX_BYTES} bytes"
            )

        untracked_files: list[str] = []
        untracked_root = temp_dir / "untracked"
        copied_bytes = 0
        for relative_text, source, expected_size in entries:
            _checkpoint_remaining(deadline)
            relative = Path(relative_text)
            target = untracked_root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            if source.is_symlink():
                os.symlink(os.readlink(source), target)
                copied_bytes += expected_size
            elif source.is_file():
                copied_bytes += _copy_checkpoint_file(source, target, deadline)
            if patch_bytes + copied_bytes > CHECKPOINT_MAX_BYTES:
                raise RuntimeError(
                    f"workspace checkpoint exceeds {CHECKPOINT_MAX_BYTES} bytes"
                )
            untracked_files.append(relative_text)

        short_status = _require_complete_git_output(
            _run_git_capture(
                ["status", "--short"],
                timeout=_checkpoint_remaining(deadline),
            ),
            "git status --short",
        )

        metadata = {
            "version": 2,
            "task_id": task_id,
            "reason": reason,
            "created_at": now_iso(),
            "base_head": base_head,
            "work_branch": branch,
            "path": str(final_dir),
            "tracked_patch": "tracked.patch",
            "untracked_root": "untracked",
            "untracked_files": untracked_files,
            "tracked_patch_bytes": patch_bytes,
            "untracked_bytes": copied_bytes,
            "total_bytes": patch_bytes + copied_bytes,
            "status": short_status,
        }
        atomic_write_text(
            temp_dir / "metadata.json",
            json.dumps(metadata, indent=2, ensure_ascii=False) + "\n",
        )
        atomic_write_text(
            temp_dir / "RESTORE.txt",
            "Checkpoint base HEAD: " + base_head + "\n\n"
            "From a checkout at that base commit:\n"
            "  git apply --binary tracked.patch\n"
            "  cp -a untracked/. <repo>/   # only when untracked/ exists\n",
        )
        _checkpoint_remaining(deadline)
        _fsync_checkpoint_tree(temp_dir)
        os.replace(temp_dir, final_dir)
        fsync_directory(checkpoint_root)
        log(
            f"workspace checkpoint saved: {final_dir} "
            f"untracked={len(untracked_files)}"
        )
        return metadata
    except Exception:
        shutil.rmtree(temp_dir, ignore_errors=True)
        raise


def prepare_work(branch: str, *, task_id: str = "prepare-work") -> None:
    branch = validate_branch(branch)

    safeguard = checkpoint_worktree(task_id, reason="before-prepare")
    if safeguard is not None:
        log(f"preserved dirty workspace before prepare: {safeguard['path']}")

    for args in (
        ["git", "reset", "--hard"],
        ["git", "clean", "-fd"],
        ["git", "fetch", "origin", branch],
        ["git", "checkout", "-B", "agent-work", f"origin/{branch}"],
        ["git", "reset", "--hard", f"origin/{branch}"],
        ["git", "clean", "-fd"],
    ):
        result = process(args, WORK, timeout=300)
        if result["exit_code"] != 0:
            raise RuntimeError(
                f"prepare_work failed: {' '.join(args)}\n{result['output']}"
            )


def cleanup_work() -> None:
    for args in (
        ["git", "reset", "--hard"],
        ["git", "clean", "-fd"],
    ):
        result = process(args, WORK, timeout=300)
        if result["exit_code"] != 0:
            raise RuntimeError(f"cleanup failed: {' '.join(args)}\n{result['output']}")


def safe_work_path(relative: str) -> Path:
    if not relative or "\x00" in relative:
        raise ValueError("invalid write path")
    candidate = (WORK / relative).resolve()
    root = WORK.resolve()
    if candidate == root or root not in candidate.parents:
        raise ValueError(f"path escapes workspace: {relative!r}")
    return candidate


def apply_patch(patch: str) -> dict[str, Any]:
    if not patch.strip():
        return {"applied": False, "reason": "empty_patch"}

    check = process(
        ["git", "apply", "--check", "--whitespace=nowarn", "-"],
        WORK,
        timeout=120,
        input_text=patch,
    )
    if check["exit_code"] != 0:
        return {
            "applied": False,
            "reason": "git_apply_check_failed",
            "check": check,
        }

    applied = process(
        ["git", "apply", "--whitespace=nowarn", "-"],
        WORK,
        timeout=120,
        input_text=patch,
    )
    return {
        "applied": applied["exit_code"] == 0,
        "apply": applied,
    }


def apply_writes(writes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for item in writes:
        if not isinstance(item, dict):
            raise ValueError("write items must be objects")
        relative = item.get("path")
        if not isinstance(relative, str):
            raise ValueError("write path must be a string")
        content = item.get("content")
        if not isinstance(content, str):
            raise ValueError(f"write content must be string for {relative!r}")
        target = safe_work_path(relative)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        results.append(
            {
                "path": relative,
                "bytes": len(content.encode("utf-8")),
            }
        )
        log(f"wrote file: {relative}")
    return results


def apply_deletes(deletes: list[str]) -> list[str]:
    results: list[str] = []
    for relative in deletes:
        if not isinstance(relative, str):
            raise ValueError("delete paths must be strings")
        target = safe_work_path(relative)
        if target.is_dir():
            raise ValueError(f"refusing directory delete: {relative!r}")
        if target.exists():
            target.unlink()
            log(f"deleted file: {relative}")
        results.append(str(relative))
    return results


def command_timeout_for(task: dict[str, Any]) -> int:
    raw = task.get("command_timeout", COMMAND_TIMEOUT)
    if not isinstance(raw, int) or isinstance(raw, bool):
        raise ValueError(f"invalid command_timeout: {raw!r}") from None
    timeout = raw
    if timeout < 1 or timeout > MAX_COMMAND_TIMEOUT:
        raise ValueError(
            f"command_timeout must be 1..{MAX_COMMAND_TIMEOUT}, got {timeout}"
        )
    return timeout


def _validate_stage_items(value: Any, field: str) -> None:
    if not isinstance(value, list):
        raise ValueError(f"{field} must be a list")
    for item in value:
        if not isinstance(item, dict):
            raise ValueError(f"{field} items must be objects")
        name = item.get("name")
        command = item.get("command")
        if not isinstance(name, str) or not name.strip():
            raise ValueError(f"{field} item name must be a non-empty string")
        if not isinstance(command, str) or not command.strip():
            raise ValueError(f"{field} item command must be a non-empty string")
        if "output_policy" in item:
            output_policy = item.get("output_policy")
            if not isinstance(output_policy, str) or output_policy not in OUTPUT_POLICIES:
                allowed = ", ".join(sorted(OUTPUT_POLICIES))
                raise ValueError(
                    f"{field} item output_policy must be one of: {allowed}"
                )
        if "timeout" in item:
            raw_timeout = item.get("timeout")
            if not isinstance(raw_timeout, int) or isinstance(raw_timeout, bool):
                raise ValueError(f"{field} item timeout must be an integer")
            stage_timeout = raw_timeout
            if stage_timeout < 1 or stage_timeout > MAX_COMMAND_TIMEOUT:
                raise ValueError(
                    f"{field} item timeout must be 1..{MAX_COMMAND_TIMEOUT}, "
                    f"got {stage_timeout}"
                )


def _workflow_policy_for(task: dict[str, Any]) -> str | None:
    if "workflow_policy" not in task:
        return None
    policy = task["workflow_policy"]
    if policy != EFFICIENT_VERIFICATION_POLICY:
        raise ValueError(f"unsupported workflow_policy: {policy!r}")
    return policy


def _validate_efficient_verification_policy(
    task: dict[str, Any],
    steps: list[dict[str, Any]],
    verify_steps: list[dict[str, Any]],
) -> None:
    legacy_fields = [
        field for field in ("commands", "verify_commands") if field in task
    ]
    if legacy_fields:
        fields = " and ".join(legacy_fields)
        raise ValueError(
            f"workflow_policy {EFFICIENT_VERIFICATION_POLICY!r} requires structured "
            f"stages; {fields} are not allowed"
        )

    for field, items in (("steps", steps), ("verify_steps", verify_steps)):
        for item in items:
            level = item.get("verification_level")
            if not isinstance(level, str) or level not in VERIFICATION_LEVELS:
                allowed = ", ".join(sorted(VERIFICATION_LEVELS))
                raise ValueError(
                    f"{field} item verification_level must be one of: {allowed}"
                )

    if any(item["verification_level"] == "full" for item in steps):
        raise ValueError("steps verification_level must be work or focused, not full")
    if any(item["verification_level"] == "work" for item in verify_steps):
        raise ValueError(
            "verify_steps verification_level must be focused or full, not work"
        )

    full_count = sum(
        item["verification_level"] == "full" for item in verify_steps
    )
    if full_count != 1:
        raise ValueError(
            "efficient-verification-v1 requires exactly one full verification stage"
        )
    if verify_steps[-1]["verification_level"] != "full":
        raise ValueError("the full verification stage must be the final stage")


def stage_plan_for(task: dict[str, Any]) -> list[dict[str, Any]]:
    """Return the one ordered execution plan used by legacy and staged tasks."""
    workflow_policy = _workflow_policy_for(task)
    commands = task.get("commands", [])
    verify_commands = task.get("verify_commands", [])
    steps = task.get("steps", [])
    verify_steps = task.get("verify_steps", [])
    _validate_stage_items(steps, "steps")
    _validate_stage_items(verify_steps, "verify_steps")

    if workflow_policy == EFFICIENT_VERIFICATION_POLICY:
        _validate_efficient_verification_policy(task, steps, verify_steps)

    if steps and commands:
        raise ValueError("steps and commands cannot both be non-empty")
    if verify_steps and verify_commands:
        raise ValueError("verify_steps and verify_commands cannot both be non-empty")

    primary: list[dict[str, Any]] = []
    if steps:
        for item in steps:
            entry = {
                "name": str(item["name"]),
                "command": str(item["command"]),
                "stage_timeout": int(item["timeout"]) if "timeout" in item else None,
            }
            if workflow_policy is not None:
                entry["verification_level"] = str(item["verification_level"])
            if "output_policy" in item:
                entry["output_policy"] = str(item["output_policy"])
            primary.append(entry)
    else:
        primary = [
            {"name": f"command-{index}", "command": str(command), "stage_timeout": None}
            for index, command in enumerate(commands, 1)
        ]

    verification: list[dict[str, Any]] = []
    if verify_steps:
        for item in verify_steps:
            entry = {
                "name": str(item["name"]),
                "command": str(item["command"]),
                "stage_timeout": int(item["timeout"]) if "timeout" in item else None,
            }
            if workflow_policy is not None:
                entry["verification_level"] = str(item["verification_level"])
            if "output_policy" in item:
                entry["output_policy"] = str(item["output_policy"])
            verification.append(entry)
    else:
        verification = [
            {
                "name": f"verification-{index}",
                "command": str(command),
                "stage_timeout": None,
            }
            for index, command in enumerate(verify_commands, 1)
        ]

    total = len(primary) + len(verification)
    plan: list[dict[str, Any]] = []
    stage_index = 1
    for phase, entries in (("commands", primary), ("verification", verification)):
        for entry in entries:
            stage = {
                "stage_name": entry["name"],
                "stage_index": stage_index,
                "stage_total": total,
                "stage_phase": phase,
                "command": entry["command"],
            }
            if entry["stage_timeout"] is not None:
                stage["stage_timeout"] = entry["stage_timeout"]
            if "verification_level" in entry:
                stage["verification_level"] = entry["verification_level"]
            if "output_policy" in entry:
                stage["output_policy"] = entry["output_policy"]
            plan.append(stage)
            stage_index += 1
    return plan


def run_command_list(
    commands: list[str],
    timeout: int,
    previous: dict[str, dict[str, Any]] | None = None,
    stages: list[dict[str, Any]] | None = None,
    *,
    runner: CommandRunner | None = None,
) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    history = {} if previous is None else dict(previous)
    results: list[dict[str, Any]] = []
    command_runner = run_command if runner is None else runner

    for offset, command in enumerate(commands):
        if not isinstance(command, str) or not command.strip():
            raise ValueError(f"invalid command: {command!r}")
        stage = stages[offset] if stages is not None else None

        effective_timeout = timeout
        if stage is not None and stage.get("stage_timeout") is not None:
            effective_timeout = int(stage["stage_timeout"])
        result = command_runner(command, effective_timeout, stage=stage)
        if stage is not None:
            result.update(stage)
        history[command] = result
        results.append(result)
        if result["exit_code"] != 0:
            break

    return results, history


def git_snapshot() -> tuple[dict[str, Any], dict[str, Any]]:
    status = process(["git", "status", "--short"], WORK)
    diff = process(["git", "diff", "--no-ext-diff"], WORK)
    return status, diff


def _record_finalization_failure(
    result: dict[str, Any],
    reason: str,
    error_field: str,
    exc: Exception,
) -> None:
    error = f"{type(exc).__name__}: {exc}"
    previous = result.get("primary_failure_reason") or result.get("failure_reason")
    causes = result.setdefault("failure_causes", [])
    if previous and not any(item.get("reason") == previous for item in causes):
        causes.append({"reason": previous})
    causes.append({"reason": reason, "error": error})
    if previous:
        result["primary_failure_reason"] = previous
    result["status"] = "failed"
    result["failure_reason"] = reason
    result[error_field] = error
    result["finished_at"] = now_iso()


def process_task(
    task: dict[str, Any],
    *,
    command_runner: CommandRunner | None = None,
) -> dict[str, Any]:
    task_id = task.get("id")
    if not isinstance(task_id, str) or not task_id:
        raise ValueError("task id must be a non-empty string")
    mode = task.get("mode", "commands")
    if mode != "commands":
        raise ValueError(
            f"unsupported mode {mode!r}; this daemon executes deterministic commands only"
        )

    branch = task.get("work_branch", "main")
    if not isinstance(branch, str):
        raise ValueError("work_branch must be a string")
    raw_allow_write = task.get("allow_write", False)
    if not isinstance(raw_allow_write, bool):
        raise ValueError("allow_write must be a boolean")
    allow_write = raw_allow_write
    patch = task.get("patch")
    writes = task.get("writes", [])
    deletes = task.get("deletes", [])
    commands = task.get("commands", [])
    verify_commands = task.get("verify_commands", [])
    steps = task.get("steps", [])
    verify_steps = task.get("verify_steps", [])

    if patch is not None and not isinstance(patch, str):
        raise ValueError("patch must be a string")
    if not isinstance(writes, list):
        raise ValueError("writes must be a list")
    if not isinstance(deletes, list):
        raise ValueError("deletes must be a list")
    if not isinstance(commands, list):
        raise ValueError("commands must be a list")
    if not isinstance(verify_commands, list):
        raise ValueError("verify_commands must be a list")
    if not isinstance(steps, list):
        raise ValueError("steps must be a list")
    if not isinstance(verify_steps, list):
        raise ValueError("verify_steps must be a list")
    for field, items in (
        ("commands", commands),
        ("verify_commands", verify_commands),
    ):
        if any(not isinstance(item, str) or not item.strip() for item in items):
            raise ValueError(f"{field} items must be non-empty strings")

    stage_plan = stage_plan_for(task)
    primary_stages = [stage for stage in stage_plan if stage["stage_phase"] == "commands"]
    verification_stages = [stage for stage in stage_plan if stage["stage_phase"] == "verification"]

    requested_edit = bool((patch and patch.strip()) or writes or deletes)
    if requested_edit and not allow_write:
        raise ValueError("task requests edits but allow_write is false")

    timeout = command_timeout_for(task)
    runner = run_command if command_runner is None else command_runner
    started_at = now_iso()
    log(
        f"starting task {task_id} branch={branch} "
        f"mode=commands write={allow_write} timeout={timeout}s"
    )

    from local_agent.foundation import attached_checkout
    branch, original_fingerprint = attached_checkout.prepare_task_workspace(
        sys.modules[__name__], task, branch)
    result: dict[str, Any] = {
        "id": task_id,
        "status": "failed",
        "mode": "commands",
        "work_branch": branch,
        "allow_write": allow_write,
        "started_at": started_at,
        "command_timeout": timeout,
        "edits": {},
        "commands": [],
        "verification": [],
        "stages": [],
    }

    command_history: dict[str, dict[str, Any]] = {}
    failure_reason: str | None = None

    try:
        if patch and patch.strip():
            patch_result = apply_patch(patch)
            result["edits"]["patch"] = patch_result
            if not patch_result.get("applied"):
                failure_reason = "patch_failed"

        if failure_reason is None and writes:
            result["edits"]["writes"] = apply_writes(writes)

        if failure_reason is None and deletes:
            result["edits"]["deletes"] = apply_deletes(
                deletes
            )

        if failure_reason is None and commands:
            command_results, command_history = run_command_list(
                commands,
                timeout,
                command_history,
                primary_stages,
                runner=runner,
            )
            result["commands"] = command_results
            if command_results and command_results[-1]["exit_code"] != 0:
                failure_reason = "command_failed"

        if failure_reason is None and steps:
            command_results, command_history = run_command_list(
                [item["command"] for item in steps],
                timeout,
                command_history,
                primary_stages,
                runner=runner,
            )
            result["commands"] = command_results
            if command_results and command_results[-1]["exit_code"] != 0:
                failure_reason = "command_failed"

        if failure_reason is None and verify_commands:
            verification_results, command_history = run_command_list(
                verify_commands,
                timeout,
                command_history,
                verification_stages,
                runner=runner,
            )
            result["verification"] = verification_results
            if verification_results and verification_results[-1]["exit_code"] != 0:
                failure_reason = "verification_failed"

        if failure_reason is None and verify_steps:
            verification_results, command_history = run_command_list(
                [item["command"] for item in verify_steps],
                timeout,
                command_history,
                verification_stages,
                runner=runner,
            )
            result["verification"] = verification_results
            if verification_results and verification_results[-1]["exit_code"] != 0:
                failure_reason = "verification_failed"

        executed = result["commands"] + result["verification"]
        result["stages"] = [
            {
                "stage_name": item.get("stage_name"),
                "stage_index": item.get("stage_index"),
                "stage_total": item.get("stage_total"),
                "stage_phase": item.get("stage_phase"),
                **(
                    {"verification_level": item["verification_level"]}
                    if "verification_level" in item
                    else {}
                ),
                "outcome": "not_started" if item.get("not_started") else (
                    "passed" if item.get("exit_code") == 0 else "failed"
                ),
                "elapsed_seconds": item.get("elapsed_seconds"),
            }
            for item in executed
        ]

        status, diff = git_snapshot()
        result["git_status"] = status
        result["git_diff"] = diff

        if status["exit_code"] != 0 or diff["exit_code"] != 0:
            failure_reason = failure_reason or "git_snapshot_failed"

        changed = attached_checkout.workspace_changed(WORK, original_fingerprint, status)
        if not allow_write and changed:
            failure_reason = failure_reason or "unexpected_worktree_changes"

        if requested_edit and not changed:
            failure_reason = failure_reason or "requested_edit_produced_no_diff"

        if failure_reason is None:
            result["status"] = "done"
        else:
            result["failure_reason"] = failure_reason

        result["finished_at"] = now_iso()
        return result

    except Exception as exc:
        result["status"] = "failed"
        result["failure_reason"] = "exception"
        result["error"] = f"{type(exc).__name__}: {exc}"
        result["traceback"] = traceback.format_exc()
        try:
            status, diff = git_snapshot()
            result["git_status"] = status
            result["git_diff"] = diff
        except Exception:
            pass
        result["finished_at"] = now_iso()
        return result
    finally:
        checkpoint_ok = True
        try:
            checkpoint = checkpoint_worktree(task_id, reason="task-exit")
            if checkpoint is not None:
                result["workspace_checkpoint"] = checkpoint
        except Exception as exc:
            checkpoint_ok = False
            _record_finalization_failure(
                result,
                "workspace_checkpoint_failed",
                "checkpoint_error",
                exc,
            )
            log(
                "workspace checkpoint failed; cleanup skipped to preserve dirty state: "
                f"{type(exc).__name__}: {exc}"
            )
        if checkpoint_ok and original_fingerprint is None:
            try:
                cleanup_work()
            except Exception as exc:
                _record_finalization_failure(
                    result,
                    "workspace_cleanup_failed",
                    "cleanup_error",
                    exc,
                )
                log(
                    "workspace cleanup failed after checkpoint preservation: "
                    f"{type(exc).__name__}: {exc}"
                )


def publish_control_json(
    relative: str | Path,
    payload: dict[str, Any],
    *,
    commit_message: str,
) -> None:
    """Publish one path-scoped JSON object on the bound control branch."""
    relative_path = Path(relative)
    if (
        relative_path.is_absolute()
        or not relative_path.parts
        or relative_path.parts[0] != ".agent"
        or ".." in relative_path.parts
        or "\x00" in str(relative_path)
    ):
        raise ValueError("control JSON path must stay below .agent")
    if (
        not isinstance(commit_message, str)
        or not commit_message.strip()
        or len(commit_message) > 200
        or "\n" in commit_message
        or "\r" in commit_message
    ):
        raise ValueError("control JSON commit message is invalid")

    with CONTROL_GIT_LOCK:
        from local_agent.repository.host_routing import reject_host_namespace_symlinks

        reject_host_namespace_symlinks(CONTROL, relative_path)
        root = CONTROL.resolve()
        path = (CONTROL / relative_path).resolve()
        if root not in path.parents:
            raise ValueError("control JSON path escapes control repository")
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(
            path,
            json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
        )

        scoped = str(path.relative_to(root))
        add = process(
            ["git", "add", "--", scoped],
            CONTROL,
            log_commands=False,
        )
        if add["exit_code"] != 0:
            raise RuntimeError(storage.git_failure_diagnostic(add))

        commit = process(
            ["git", "commit", "-m", commit_message, "--", scoped],
            CONTROL,
            log_commands=False,
        )
        if commit["exit_code"] != 0:
            status = process(
                ["git", "status", "--short", "--", scoped],
                CONTROL,
                log_commands=False,
            )
            if status["exit_code"] != 0 or status["output"].strip():
                raise RuntimeError(storage.git_failure_diagnostic(commit))

        storage.publish_control_commit(
            sys.modules[__name__],
            timeout=180,
            log_commands=False,
        )

    log(f"published control JSON {relative_path}")


def publish_result(task_id: str, result: dict[str, Any]) -> None:
    """Publish a durable result with quiet successful control-plane Git plumbing."""
    publish_control_json(
        Path(RESULTS_DIR) / f"{task_id}.json",
        result,
        commit_message=f"Agent result: {task_id}",
    )
