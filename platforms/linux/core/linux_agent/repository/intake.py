"""Explicit, isolated Git refresh for an optional project mailbox.

No execution, claims, project checkout mutation or scheduler. The intake owns
only its disposable checkout in the selected installation's private state.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
from dataclasses import replace
from pathlib import Path

from linux_agent.foundation.process import atomic_write_text
from linux_agent.repository.journal import ProjectJournal
from linux_agent.repository.mailbox import ProjectCandidate, ProjectMailbox

_OID = re.compile(r"^[0-9a-f]{40,64}$")


class ProjectGitUnavailable(ValueError):
    """Selected project remote cannot currently be refreshed."""


class ProjectTaskStale(ValueError):
    """The original immutable candidate is not pending at the fresh remote tip."""


class ProjectGitIntake:
    """Re-read project tasks via a dedicated clone, without touching B2 work.

    The selected project must be explicitly configured and its exact origin
    independently supplied. Neither registry presence nor constructing this
    helper enables an executor.
    """

    def __init__(self, journal: ProjectJournal, *, origin: str):
        if not isinstance(origin, str) or not origin:
            raise ValueError("explicit project Git origin required")
        self.journal = journal
        self.origin = origin
        self.project = journal.project
        self.root = journal.state / "project-intakes" / self.project.repository_id
        self.control = self.root / "control"
        self.marker = self.root / "OWNED_BY_PROJECT_INTAKE.json"

    @staticmethod
    def _private(path: Path) -> None:
        if path.is_symlink():
            raise ValueError("project intake path is symlinked")
        if not path.exists():
            path.mkdir(mode=0o700)
        info = path.stat()
        if not path.is_dir() or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ValueError("project intake directory must be owner-private")

    def _git(self, *args: str, cwd: Path | None = None, timeout: int = 30) -> bytes:
        try:
            process = subprocess.run(
                ["git", *(["-C", str(cwd)] if cwd else []), *args],
                capture_output=True, check=False, timeout=timeout,
                env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ProjectGitUnavailable("project intake Git unavailable") from exc
        if process.returncode:
            raise ProjectGitUnavailable("project intake Git unavailable or invalid")
        return process.stdout

    def initialize(self) -> None:
        self._private(self.journal.state)
        self._private(self.journal.state / "project-intakes")
        self._private(self.root)
        identity = {
            "version": 1, "origin": self.origin,
            "branch": self.project.control_branch,
            "repository_id": self.project.repository_id,
            "agent_binding": self.project.agent_binding,
            "host_id": self.journal.host_id,
            "installation_id": self.journal.installation_id,
            "checkout": str(self.control),
        }
        if not self.control.exists() and not self.control.is_symlink():
            if self.marker.exists() or self.marker.is_symlink():
                raise ValueError("project intake marker without checkout")
            self._git("clone", "--no-checkout", "--single-branch",
                      "--branch=" + self.project.control_branch,
                      self.origin, str(self.control), cwd=self.root, timeout=90)
            atomic_write_text(self.marker, json.dumps(identity, sort_keys=True) + "\n")
        if (self.control.is_symlink() or not self.control.is_dir()
                or (self.control / ".git").is_symlink()
                or not (self.control / ".git").is_dir()
                or self.marker.is_symlink() or not self.marker.is_file()):
            raise ValueError("unsafe or unowned project intake checkout")
        if self.marker.stat().st_mode & 0o077:
            raise ValueError("unsafe project intake marker permissions")
        try:
            actual = json.loads(self.marker.read_bytes())
        except (OSError, ValueError) as exc:
            raise ValueError("invalid project intake ownership marker") from exc
        if actual != identity:
            raise ValueError("project intake ownership mismatch")
        if self._git("remote", "get-url", "origin", cwd=self.control).decode().strip() != self.origin:
            raise ValueError("project intake origin changed")

    def refresh(self) -> ProjectMailbox:
        """Fetch exact branch, then return a pinned mailbox on OUR clone."""
        self.initialize()
        branch = self.project.control_branch
        self._git("fetch", "--no-tags", "origin",
                  "+refs/heads/" + branch + ":refs/remotes/origin/" + branch,
                  cwd=self.control, timeout=90)
        sha = self._git("rev-parse", "--verify",
                        "refs/remotes/origin/" + branch + "^{commit}",
                        cwd=self.control).decode().strip()
        if not _OID.fullmatch(sha):
            raise ValueError("invalid fetched project commit")
        project = replace(self.project, control=self.control)
        mailbox = ProjectMailbox(
            project, host_id=self.journal.host_id,
            installation_id=self.journal.installation_id,
            expected_origin=self.origin,
        )
        if mailbox.verify_snapshot() != sha:
            raise ValueError("project intake snapshot changed during refresh")
        return mailbox

    def pending_tasks(self, *, claimed: set[str] | None = None) -> list[ProjectCandidate]:
        return self.refresh().pending_tasks(claimed=claimed)

    def revalidate(self, candidate: ProjectCandidate) -> None:
        """Fresh remote recheck for admission's final pre-fsync callback.

        If the task was edited, removed, already answered or Git is down,
        refuse claim. No stale local remote-tracking ref can authorize it.
        """
        for current in self.pending_tasks():
            if current.task_id == candidate.task_id:
                if current.raw != candidate.raw or current.digest != candidate.digest:
                    raise ProjectTaskStale("project task changed before claim")
                return
        raise ProjectTaskStale("project task no longer pending before claim")
