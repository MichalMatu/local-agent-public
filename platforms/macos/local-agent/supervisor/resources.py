"""External-resource admission and inherited flock handling for parallel workers."""

from __future__ import annotations

import contextlib
import fcntl
import os
from collections.abc import Iterator, MutableMapping
from pathlib import Path
from typing import TextIO

from local_agent.foundation.process import RESOURCE_LEASE_FDS_ENV, execution_lease_path
from local_agent.runtime.task_contract import task_resources_for


class MachineResourceBusy(RuntimeError):
    def __init__(self, resource: str) -> None:
        super().__init__(f"machine resource is busy: {resource}")
        self.resource = resource


def resource_lock_dir(state_dir: Path) -> Path:
    return state_dir / "locks" / "machine-resources"


def task_resources(task: dict[str, object]) -> tuple[str, ...]:
    """Return the explicit validated external-resource contract for one task."""
    return task_resources_for(task)


def _acquire_flock(handle: TextIO, operation: int, *, resource: str) -> None:
    try:
        fcntl.flock(handle.fileno(), operation | fcntl.LOCK_NB)
    except BlockingIOError:
        raise MachineResourceBusy(resource) from None


def _restore_env_value(
    target: MutableMapping[str, str],
    name: str,
    previous: str | None,
) -> None:
    if previous is None:
        target.pop(name, None)
    else:
        target[name] = previous


@contextlib.contextmanager
def inherit_resource_fds(
    handles: list[TextIO],
    *,
    process_env: MutableMapping[str, str],
    command_env: MutableMapping[str, str],
) -> Iterator[None]:
    """Expose held lease FDs to descendants and restore both environments exactly."""
    existing_raw = process_env.get(RESOURCE_LEASE_FDS_ENV, "").strip()
    existing = [item for item in existing_raw.split(",") if item]
    combined = existing + [str(handle.fileno()) for handle in handles]
    value = ",".join(dict.fromkeys(combined))

    previous_process = process_env.get(RESOURCE_LEASE_FDS_ENV)
    previous_command = command_env.get(RESOURCE_LEASE_FDS_ENV)
    process_env[RESOURCE_LEASE_FDS_ENV] = value
    command_env[RESOURCE_LEASE_FDS_ENV] = value
    try:
        yield
    finally:
        _restore_env_value(process_env, RESOURCE_LEASE_FDS_ENV, previous_process)
        _restore_env_value(command_env, RESOURCE_LEASE_FDS_ENV, previous_command)


@contextlib.contextmanager
def machine_resource_lease(
    task: dict[str, object],
    *,
    lock_dir: Path,
    command_env: MutableMapping[str, str],
    process_env: MutableMapping[str, str] | None = None,
) -> Iterator[tuple[str, ...]]:
    """Acquire external resources without waiting after task selection."""
    resources = task_resources(task)
    lock_dir.mkdir(parents=True, exist_ok=True)
    resolved_process_env = os.environ if process_env is None else process_env

    gate = (lock_dir / "arbitration-gate.lock").open("a+", encoding="utf-8")
    machine = (lock_dir / "machine.lock").open("a+", encoding="utf-8")
    resource_handles: list[TextIO] = []
    try:
        _acquire_flock(gate, fcntl.LOCK_EX, resource="arbitration-gate")
        machine_mode = fcntl.LOCK_EX if resources == ("machine",) else fcntl.LOCK_SH
        _acquire_flock(machine, machine_mode, resource="machine")

        if resources != ("machine",):
            for resource in resources:
                path = execution_lease_path(lock_dir, f"resource:{resource}")
                handle = path.open("a+", encoding="utf-8")
                try:
                    _acquire_flock(handle, fcntl.LOCK_EX, resource=resource)
                except BaseException:
                    handle.close()
                    raise
                resource_handles.append(handle)

        fcntl.flock(gate.fileno(), fcntl.LOCK_UN)
        gate.close()

        inherited = [machine, *resource_handles]
        with inherit_resource_fds(
            inherited,
            process_env=resolved_process_env,
            command_env=command_env,
        ):
            yield resources
    finally:
        for handle in reversed(resource_handles):
            with contextlib.suppress(OSError):
                handle.close()
        with contextlib.suppress(OSError):
            machine.close()
        if not gate.closed:
            with contextlib.suppress(OSError):
                gate.close()
