from __future__ import annotations

import copy
import json
import os
import shutil
import tempfile
from pathlib import Path
from typing import Any

from local_agent.foundation.process import atomic_write_text, fsync_directory
from local_agent.runtime.task_contract import TASK_PAYLOAD_REF_KEY, validate_task


def _payload_ref(relative_path: str) -> dict[str, str]:
    return {TASK_PAYLOAD_REF_KEY: relative_path}


def _atomic_write_text_no_clobber(path: Path, text: str) -> None:
    """Atomically publish one complete UTF-8 file without replacing an existing path."""
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temp_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    temp_path = Path(temp_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temp_path, path)
        except FileExistsError as exc:
            raise FileExistsError(f"task manifest already exists: {path}") from exc
        fsync_directory(path.parent)
    finally:
        temp_path.unlink(missing_ok=True)


def externalize_task_payloads(
    task: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, str]]:
    """Return a validated task envelope plus escape-safe external text payloads."""
    canonical = copy.deepcopy(task)
    validate_task(canonical)
    manifest = copy.deepcopy(canonical)
    task_id = str(manifest["id"])
    payload_prefix = f"{task_id}.payload"
    payloads: dict[str, str] = {}

    def store(relative_name: str, content: str) -> dict[str, str]:
        relative_path = f"{payload_prefix}/{relative_name}"
        payloads[relative_path] = content
        return _payload_ref(relative_path)

    patch = manifest.get("patch")
    if isinstance(patch, str):
        manifest["patch"] = store("patch.diff", patch)

    for index, item in enumerate(manifest.get("writes", []), start=1):
        content = item.get("content")
        if isinstance(content, str):
            item["content"] = store(f"writes/{index:03d}.txt", content)

    for field in ("commands", "verify_commands"):
        commands = manifest.get(field, [])
        directory = "commands" if field == "commands" else "verify-commands"
        for index, command in enumerate(commands, start=1):
            if isinstance(command, str):
                commands[index - 1] = store(f"{directory}/{index:03d}.sh", command)

    for field in ("steps", "verify_steps"):
        steps = manifest.get(field, [])
        directory = "steps" if field == "steps" else "verify-steps"
        for index, item in enumerate(steps, start=1):
            command = item.get("command")
            if isinstance(command, str):
                item["command"] = store(f"{directory}/{index:03d}.sh", command)

    return manifest, payloads


def write_task_bundle(
    tasks_dir: Path,
    task: dict[str, Any],
    *,
    manifest_name: str | None = None,
) -> Path:
    """Preflight and atomically expose a task manifest after all payloads exist."""
    manifest, payloads = externalize_task_payloads(task)
    task_id = str(manifest["id"])
    name = manifest_name or f"{task_id}.json"
    if Path(name).name != name or not name.endswith(".json"):
        raise ValueError("manifest_name must be one .json filename")

    tasks_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = tasks_dir / name
    payload_root = tasks_dir / f"{task_id}.payload"
    if manifest_path.exists():
        raise FileExistsError(f"task manifest already exists: {manifest_path}")
    try:
        payload_root.mkdir(parents=False, exist_ok=False)
    except FileExistsError as exc:
        raise FileExistsError(
            f"task payload directory already exists: {payload_root}"
        ) from exc

    try:
        for relative_path, content in payloads.items():
            target = tasks_dir / relative_path
            target.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_text(target, content)
        _atomic_write_text_no_clobber(
            manifest_path,
            json.dumps(manifest, indent=2, ensure_ascii=False) + "\n",
        )
    except Exception:
        shutil.rmtree(payload_root, ignore_errors=True)
        raise
    return manifest_path
