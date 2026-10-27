"""Optional project pre-effect admission and STOP, not a project executor.

This module is NOT connected to the Linux Agent loop. A future project worker
must provide fresh immutable Git intake, a selected installation daemon lock,
bounded execution, live STOP polling and verified terminal publication itself.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import stat
from contextlib import closing, contextmanager, nullcontext
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterator

from linux_agent.agent import daemon_lock
from linux_agent.execution_recovery import recover_task_lease
from linux_agent.foundation.process import (
    LEASE_FDS_ENV, LEASE_KEYS_DIGEST_ENV, acquire_execution_leases,
    atomic_write_text, execution_lease_path,
)
from linux_agent.repository.journal import ProjectJournal, _pairs
from linux_agent.repository.mailbox import ProjectCandidate
from linux_agent.repository.projects import resolve_project_identity
from linux_agent.resources import machine_resource_lease, resource_lock_dir
from linux_agent.runtime.task_contract import task_resources_for


class ProjectStopRequested(RuntimeError):
    """Durable STOP prevents new project claims, with no automatic resume."""


@dataclass(frozen=True)
class ProjectAdmission:
    task_id: str
    task_digest: str
    resources: tuple[str, ...]
    stop_path: Path

    def stop_requested(self) -> bool:
        """Future executor must poll STOP and terminate active children."""
        if self.stop_path.is_symlink():
            raise ValueError("unsafe project STOP symlink")
        return self.stop_path.exists()


class ProjectAdmissionGate:
    """Reserve resources and the inherited task-specific lease before claim.

    Does not spawn any process. Context lifetime is the future command's
    mandatory lease lifetime; do NOT extract/return its FDs after exit.
    """

    def __init__(self, journal: ProjectJournal):
        self.journal = journal
        self.state = journal.state
        self.stop_path = journal.root / "STOP.json"
        self._stop_gate_path = journal.root / "stop-admission.lock"

    @contextmanager
    def _stop_gate(self, *, exclusive: bool) -> Iterator[None]:
        """Serialize a durable STOP acknowledgment with the pre-effect claim.

        This is a local per-project flock, NOT another Git approval mechanism.
        Neither STOP nor claim may race past each other between final check
        and fsync. Never hold it for the future task execution.
        """
        self.journal._private_dir(self.journal.root, create=True)
        flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(self._stop_gate_path, flags, 0o600)
        try:
            info = os.fstat(fd)
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                    or stat.S_IMODE(info.st_mode) & 0o077):
                raise ValueError("unsafe project STOP admission lock")
            fcntl.flock(fd, fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
            try:
                yield
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

    def _check_stop(self) -> None:
        if self.stop_path.is_symlink():
            raise ValueError("unsafe project STOP symlink")
        if self.stop_path.exists():
            if not self.stop_path.is_file():
                raise ValueError("invalid project STOP path")
            raise ProjectStopRequested("project intake disabled by durable STOP")

    def request_stop(self) -> None:
        """Durable local project STOP. Does not claim ability to kill a worker."""
        with self._stop_gate(exclusive=True):
            if self.stop_path.is_symlink():
                raise ValueError("unsafe project STOP symlink")
            if self.stop_path.exists():
                if not self.stop_path.is_file():
                    raise ValueError("invalid project STOP path")
                return
            atomic_write_text(
                self.stop_path,
                json.dumps({
                    "version": 1, "repository_id": self.journal.project.repository_id,
                    "host_id": self.journal.host_id,
                    "installation_id": self.journal.installation_id,
                    "action": "stop_intake",
                }, sort_keys=True) + "\n",
            )

    def _lease_key(self, task_id: str) -> str:
        return f"project:{self.journal.project.repository_id}:task:{task_id}"

    def _lease_path(self, task_id: str) -> Path:
        self.journal._claim_path(task_id)  # strictly validate ID
        return execution_lease_path(
            self.state / "locks" / "task-execution", self._lease_key(task_id)
        )

    @contextmanager
    def admit(
        self, candidate: ProjectCandidate,
        *, preclaim_check: Callable[[ProjectCandidate], None] | None = None,
        for_rejection: bool = False,
    ) -> Iterator[ProjectAdmission]:
        """Durably claim one already-selected project task, never run it.

        Caller must hold the selected installation's lifetime daemon_lock.
        A future worker must check freshness and re-read the same task blob
        immediately before this context, and record a terminal result before
        exit; claiming without a terminal result is UNKNOWN on next recovery.
        """
        if (hashlib.sha256(candidate.raw).hexdigest() != candidate.digest
                or not isinstance(candidate.snapshot_sha, str)
                or len(candidate.snapshot_sha) not in (40, 64)
                or any(c not in "0123456789abcdef" for c in candidate.snapshot_sha)):
            raise ValueError("project task digest or immutable snapshot invalid")
        try:
            payload = json.loads(candidate.raw, object_pairs_hook=_pairs)
        except (ValueError, UnicodeDecodeError) as exc:
            raise ValueError("project task bytes invalid") from exc
        if (not isinstance(payload, dict) or payload != candidate.task
                or payload.get("id") != candidate.task_id or
                type(payload.get("version")) is not int or payload["version"] != 1):
            raise ValueError("project task candidate changed before admission")
        resolve_project_identity(payload, (self.journal.project,))
        if payload.get("target_host_id", self.journal.host_id) != self.journal.host_id:
            raise ValueError("project task host mismatch")
        if payload.get("target_installation_id", self.journal.installation_id) != self.journal.installation_id:
            raise ValueError("project task installation mismatch")
        # Invalid remote input still needs an immutable terminal decision, but
        # its unvalidated resources must never enter execution admission.
        resources = () if for_rejection else task_resources_for(payload)
        self.journal._private_dir(self.journal.root, create=True)
        self._check_stop()
        # Match established host machine/resource lock namespace and inherit
        # the same FDs into future Linux RuntimeExecutor children.
        reservation = nullcontext() if for_rejection else machine_resource_lease(
            payload, lock_dir=resource_lock_dir(self.state),
            command_env=os.environ,
        )
        with reservation:
            self._check_stop()
            with closing(acquire_execution_leases(
                self.state / "locks" / "task-execution",
                (self._lease_key(candidate.task_id),),
            )) as leases:
                before = {
                    LEASE_FDS_ENV: os.environ.get(LEASE_FDS_ENV),
                    LEASE_KEYS_DIGEST_ENV: os.environ.get(LEASE_KEYS_DIGEST_ENV),
                }
                os.environ.update(leases.environment())
                try:
                    with self._stop_gate(exclusive=False):
                        self._check_stop()
                        # Final Git freshness belongs INSIDE serialized admission.
                        # If Git fails or candidate changes, no claim is written.
                        if preclaim_check is not None:
                            preclaim_check(candidate)
                        self._check_stop()
                        self.journal.claim(candidate.task_id, candidate.digest)
                    yield ProjectAdmission(
                        candidate.task_id, candidate.digest, resources,
                        self.stop_path,
                    )
                finally:
                    for key, previous in before.items():
                        if previous is None:
                            os.environ.pop(key, None)
                        else:
                            os.environ[key] = previous

    def recover_unknown_when_supervisor_stopped(self) -> int:
        """Fence exact project task lease holders before writing UNKNOWN.

        Only run outside the active service; daemon_lock prevents concurrent
        admission by the selected host installation. No PID/name guessing.
        The process-specific lease file MUST exist for each durable claim.
        """
        with daemon_lock(self.state):
            def fence(task_id: str) -> None:
                lease = self._lease_path(task_id)
                if lease.is_symlink() or not lease.is_file():
                    raise ValueError("missing/unsafe project execution lease")
                recover_task_lease(lease)
            return self.journal.recover_unknown(fence=fence)
