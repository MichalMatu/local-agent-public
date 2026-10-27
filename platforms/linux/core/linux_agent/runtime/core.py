"""Minimal independent Linux host execution Core using copied Mac runtime primitives."""
from __future__ import annotations
from datetime import datetime, timezone
import hashlib
import os
from pathlib import Path
import subprocess
from typing import Any, Callable

from linux_agent.foundation.process import terminate_process_group
from linux_agent.runtime.task_contract import task_digest

OUTPUT_POLICIES = {"stream", "summary", "quiet"}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def log(message: str) -> None:
    # Never echo the shell payload or sensitive stdout into the daemon journal.
    print("[linux-agent] " + message[:500], flush=True)


def command_log_descriptor(command: str, metadata: dict[str, Any]) -> str:
    return "command sha256=" + hashlib.sha256(command.encode()).hexdigest()[:16]


def kill_process_group(proc: subprocess.Popen[str]) -> None:
    terminate_process_group(proc, log, grace_seconds=1.0)


def stage_plan_for(task: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {"command": command, "stage_name": f"command-{index}",
         "stage_index": index, "stage_total": len(task["commands"]),
         "stage_phase": "commands", "output_policy": task.get("output_policy", "summary")}
        for index, command in enumerate(task["commands"], 1)
    ]


class Core:
    OUTPUT_POLICIES = OUTPUT_POLICIES
    now_iso = staticmethod(now_iso)
    log = staticmethod(log)
    command_log_descriptor = staticmethod(command_log_descriptor)
    kill_process_group = staticmethod(kill_process_group)
    stage_plan_for = staticmethod(stage_plan_for)

    def __init__(self, cwd: Path):
        if not cwd.is_dir():
            raise ValueError("task cwd is not a directory")
        self.WORK = cwd
        self.ENV = dict(os.environ)

    def process_task(self, task: dict[str, Any], *, command_runner: Callable[..., dict[str, Any]]) -> dict[str, Any]:
        started = now_iso()
        commands = []
        stages = []
        failed = False
        for stage in stage_plan_for(task):
            value = command_runner(stage["command"], task.get("command_timeout", 900), stage=stage)
            commands.append(value)
            stages.append({"stage_name": stage["stage_name"], "stage_phase": "commands",
                           "exit_code": value["exit_code"], "elapsed_seconds": value["elapsed_seconds"]})
            if value["exit_code"] != 0:
                failed = True
                break
        return {
            "version": 1, "id": task["id"], "status": "failed" if failed else "done",
            "target_host_id": task["target_host_id"], "target_installation_id": task["target_installation_id"],
            "task_digest": task_digest(task), "commands": commands, "stages": stages,
            "started_at": started, "finished_at": now_iso(),
        }
