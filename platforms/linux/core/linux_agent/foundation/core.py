"""Linux host command adapter reusing the accepted Mac stage contract.

The runtime executor and POSIX process implementation are copied into
this independent product. No runtime import reaches platforms/macos.
"""
from __future__ import annotations

import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from linux_agent.config import TIMEOUTS
from linux_agent.foundation.process import terminate_process_group

COMMAND_TIMEOUT = TIMEOUTS.command_default
MAX_COMMAND_TIMEOUT = TIMEOUTS.command_max
OUTPUT_POLICIES = frozenset({"stream", "summary"})
EFFICIENT_VERIFICATION_POLICY = "efficient-verification-v1"
VERIFICATION_LEVELS = frozenset({"work", "focused", "full"})
CONTROL = Path("/nonexistent/linux-agent-payloads-disabled")
MAX_OUTPUT = 60_000


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def log(message: str) -> None:
    # Sensitive command output lives in bounded result receipts, not stdout.
    return None


def command_log_descriptor(command: str, *args: Any, **kwargs: Any) -> str:
    return "<command-redacted>"


def kill_process_group(proc: subprocess.Popen[str]) -> None:
    terminate_process_group(proc, log)


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



@dataclass
class HostCore:
    WORK: Path
    ENV: dict[str, str]

    now_iso = staticmethod(now_iso)
    log = staticmethod(log)
    command_log_descriptor = staticmethod(command_log_descriptor)
    kill_process_group = staticmethod(kill_process_group)
    stage_plan_for = staticmethod(stage_plan_for)
    OUTPUT_POLICIES = OUTPUT_POLICIES

    def process_task(self, task: dict[str, Any], *, command_runner: Any) -> dict[str, Any]:
        plan = stage_plan_for(task)
        command_timeout = command_timeout_for(task)
        result: dict[str, Any] = {
            "id": task["id"], "status": "done", "started_at": now_iso(),
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
        result["finished_at"] = now_iso()
        return result
