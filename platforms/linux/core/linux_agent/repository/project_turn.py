"""One locally opted-in project task turn in the existing Linux supervisor.

No new scheduler or service. Caller owns the installation-wide daemon flock.
Normal host-only commands retain their separate task/result namespace.
"""
from __future__ import annotations

import json
import os
import stat
import threading
from pathlib import Path
from typing import TYPE_CHECKING, Any

from linux_agent.agent import LINUX_HOST_FIELDS, validate_linux_host_subset
from linux_agent.execution_recovery import recover_task_lease
from linux_agent.git_transport import GitError
from linux_agent.repository.admission import ProjectAdmissionGate, ProjectStopRequested
from linux_agent.repository.intake import ProjectGitIntake, ProjectGitUnavailable, ProjectTaskStale
from linux_agent.repository.journal import ProjectJournal, _pairs
from linux_agent.repository.projects import load_project_registry
from linux_agent.repository.publisher import ProjectResultPublisher, ProjectPublishError
from linux_agent.resources import MachineResourceBusy
from linux_agent.runtime.core import Core
from linux_agent.runtime.executor import RuntimeExecutor
from linux_agent.runtime.task_contract import memory_limit_for, validate_task

if TYPE_CHECKING:
    from linux_agent.agent import Agent

_PROJECT_FIELDS = LINUX_HOST_FIELDS | frozenset({"repository_id", "agent_binding"})


def _local_identity(path: Path, *, host_id: str, installation_id: str) -> None:
    if not path.is_absolute() or path.is_symlink() or not path.is_file():
        raise ValueError("selected project identity file must be local and regular")
    details = path.stat()
    if (details.st_uid != os.getuid() or not stat.S_ISREG(details.st_mode)
            or details.st_mode & 0o077 or details.st_size > 4096):
        raise ValueError("project identity file must be bounded and owner-private")
    for parent in path.parents:
        if (parent / ".git").exists():
            raise ValueError("project identity may not be sourced from a Git checkout")
    try:
        actual = json.loads(path.read_bytes(), object_pairs_hook=_pairs)
    except (OSError, ValueError, UnicodeDecodeError) as exc:
        raise ValueError("invalid local project identity JSON") from exc
    if (not isinstance(actual, dict) or set(actual) != {
            "version", "host_id", "installation_id"
    } or actual["version"] != 1 or actual["host_id"] != host_id
            or actual["installation_id"] != installation_id):
        raise ValueError("local project host/installation identity mismatch")


class ProjectTurn:
    """Exactly one project selected by an explicit local service config flag."""

    def __init__(self, config: dict[str, Any], state: Path):
        selected = config.get("project_turn")
        if not isinstance(selected, dict) or set(selected) != {
            "enabled", "registry_path", "repository_id", "origin", "max_memory_mb"
        } or selected["enabled"] is not True:
            raise ValueError("project turn requires explicit local enablement")
        for field in ("registry_path", "repository_id", "origin"):
            if not isinstance(selected[field], str) or not selected[field]:
                raise ValueError("project turn missing exact selected " + field)
        memory_cap = selected["max_memory_mb"]
        if type(memory_cap) is not int or not 64 <= memory_cap <= 4096:
            raise ValueError("invalid locally selected project memory cap")
        registry = Path(selected["registry_path"])
        if not registry.is_absolute():
            raise ValueError("project registry must be an explicit absolute path")
        self.registry_path = registry
        self.selected_repository_id = selected["repository_id"]
        projects = load_project_registry(
            registry, home=Path.home(), expected_host_id=config["host_id"])
        matching = [p for p in projects if p.repository_id == selected["repository_id"]]
        if len(matching) != 1:
            raise ValueError("selected project not locally enabled")
        self.project = matching[0]
        if self.project.host_identity_file is None:
            raise ValueError("selected project identity file required")
        _local_identity(self.project.host_identity_file,
                        host_id=config["host_id"],
                        installation_id=config["installation_id"])
        self.journal = ProjectJournal(
            state, self.project, host_id=config["host_id"],
            installation_id=config["installation_id"])
        self.gate = ProjectAdmissionGate(self.journal)
        self.intake = ProjectGitIntake(self.journal, origin=selected["origin"])
        self.publisher = ProjectResultPublisher(self.journal, origin=selected["origin"])
        self.max_memory_mb = memory_cap

    def _revalidate_before_claim(self, candidate) -> None:
        """Recheck mutable local registry and identity before irreversible claim."""
        projects = load_project_registry(
            self.registry_path, home=Path.home(),
            expected_host_id=self.journal.host_id)
        current = [p for p in projects
                   if p.repository_id == self.selected_repository_id]
        if len(current) != 1 or current[0] != self.project:
            raise ValueError("locally enabled project configuration changed")
        _local_identity(self.project.host_identity_file,
                        host_id=self.journal.host_id,
                        installation_id=self.journal.installation_id)
        self.intake.revalidate(candidate)

    def _validate(self, raw: dict[str, Any]) -> dict[str, Any]:
        if set(raw) - _PROJECT_FIELDS:
            raise ValueError("unsupported Linux project task fields")
        if (raw.get("repository_id") != self.project.repository_id
                or raw.get("agent_binding") != self.project.agent_binding):
            raise ValueError("project task binding changed")
        if (raw.get("target_host_id") != self.journal.host_id
                or raw.get("target_installation_id") != self.journal.installation_id):
            raise ValueError("project task must name exact host and installation")
        cwd = raw.get("cwd", str(self.project.work))
        if (not isinstance(cwd, str) or Path(cwd) != self.project.work
                or self.project.work.is_symlink()
                or not self.project.work.is_dir()):
            raise ValueError("project commands require the selected existing work directory")
        runtime = {key: value for key, value in raw.items()
                   if key in LINUX_HOST_FIELDS}
        runtime["cwd"] = cwd
        validate_linux_host_subset(runtime)
        validate_task(runtime)
        if memory_limit_for(runtime) > self.max_memory_mb:
            raise ValueError("project task exceeds local machine memory cap")
        return runtime

    def _recover_under_daemon_lock(self) -> int:
        """The caller must own Agent's existing installation daemon_lock."""
        def fence(task_id: str) -> None:
            lease = self.gate._lease_path(task_id)
            if lease.is_symlink() or not lease.is_file():
                raise ValueError("missing/unsafe project execution lease")
            recover_task_lease(lease)
        return self.journal.recover_unknown(fence=fence)

    @staticmethod
    def _execute(agent: Agent, runtime: dict[str, Any],
                 gate: ProjectAdmissionGate) -> dict[str, Any]:
        interruption = threading.Event()
        finished = threading.Event()
        executor = RuntimeExecutor(Core(Path(runtime["cwd"])),
                                   interruption=interruption)

        def stopped() -> bool:
            return (agent.disabled() or agent.stop_requested.is_set()
                    or gate.stop_path.is_symlink() or gate.stop_path.exists())

        def watcher() -> None:
            while not finished.wait(0.10):
                if stopped():
                    interruption.set()
                    executor.terminate_active_command()
                    return

        observer = threading.Thread(target=watcher, daemon=True,
                                    name="linux-project-stop-watcher")
        observer.start()
        try:
            if stopped():
                interruption.set()
                return {"status": "unknown", "reason": "project_stop_after_claim"}
            result = executor.process_task(runtime)
        finally:
            finished.set()
            observer.join(timeout=2)
        if interruption.is_set() or stopped():
            result["status"] = "unknown"
            result["reason"] = "project_stop_effects_uncertain"
        return result

    def once_locked(self, agent: Agent) -> dict[str, Any]:
        """One optional project turn, strictly inside Agent's single daemon lock."""
        try:
            return self._once_locked(agent)
        except ProjectGitUnavailable as exc:
            raise GitError("project Git intake unavailable; no claim written") from exc
        except ProjectPublishError as exc:
            if "conflict" in str(exc):
                raise  # An immutable result conflict is NOT a transient outage.
            raise GitError("project result publication pending; spool retained") from exc

    def _once_locked(self, agent: Agent) -> dict[str, Any]:
        self._recover_under_daemon_lock()
        self.publisher.publish_pending()  # durable spool only; never replay
        if agent.disabled() or agent.stop_requested.is_set():
            return {"status": "disabled"}
        if self.gate.stop_path.exists() or self.gate.stop_path.is_symlink():
            return {"status": "project_stopped"}
        claimed = {p.stem for p in (self.journal.root / "claims").glob("*.json")}
        for candidate in self.intake.pending_tasks(claimed=claimed):
            # Validation is entirely BEFORE the irreversible claim.
            try:
                runtime = self._validate(candidate.task)
                rejection = None
            except ValueError as exc:
                runtime = None
                rejection = {"status": "failed", "reason": "invalid_task_file",
                             "error": str(exc)[:500]}
            if agent.disabled() or agent.stop_requested.is_set():
                return {"status": "disabled"}
            try:
                with self.gate.admit(
                    candidate, preclaim_check=self._revalidate_before_claim,
                    for_rejection=rejection is not None,
                ):
                    if rejection is not None:
                        result = rejection
                    else:
                        try:
                            result = self._execute(agent, runtime, self.gate)
                        except BaseException as exc:
                            result = {"status": "unknown",
                                      "reason": "project_execution_interrupted_or_uncertain",
                                      "error_type": type(exc).__name__}
                    # Runtime canonical JSON digest is distinct from the
                    # immutable on-wire SHA-256; journal always uses raw bytes.
                    result["task_digest"] = candidate.digest
                    self.journal.record_result(candidate.task_id, candidate.digest, result)
            except MachineResourceBusy as exc:
                return {"status": "resource_busy", "resource": exc.resource,
                        "id": candidate.task_id}
            except ProjectTaskStale:
                return {"status": "task_changed_before_claim", "id": candidate.task_id}
            except ProjectStopRequested:
                return {"status": "project_stopped", "id": candidate.task_id}
            self.publisher.publish_pending()
            return {"status": result["status"], "id": candidate.task_id,
                    "digest": candidate.digest}
        return {"status": "idle"}
