"""Read-only Git project mailbox snapshot, independent of the host mailbox.

This deliberately has no fetch, claim, execution, Git push or scheduling entrypoint.
An owner-selected project checkout must be refreshed by a later supervised
transport before its immutable remote-tracking snapshot is consumed.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
from dataclasses import dataclass
from typing import Any

from linux_agent.repository.binding import canonical_agent_binding
from linux_agent.repository.projects import Project

_TASK_ID = re.compile(r"^[A-Za-z0-9._-]{1,200}$")
MAX_TASK_BYTES = 1024 * 1024


@dataclass(frozen=True, slots=True)
class ProjectCandidate:
    task_id: str
    raw: bytes
    digest: str
    task: dict[str, Any]
    snapshot_sha: str


def _pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate project task field")
        result[key] = value
    return result


class ProjectMailbox:
    """Inspect a B2 host-scoped project Git mailbox; no side effects.

    `expected_origin` is the owner's configured exact URL/path for this
    project's control Git repository, not a token/authentication mechanism.
    The caller must supply a verified refreshed remote-tracking snapshot.
    """

    def __init__(
        self, project: Project, *, host_id: str,
        installation_id: str, expected_origin: str,
    ):
        if project.host_id != host_id or not project.host_mailbox_enabled:
            raise ValueError("project host mailbox is not selected")
        if not project.host_execution_enabled or project.agent_binding is None:
            raise ValueError("project execution/binding not enabled")
        if not expected_origin or not isinstance(expected_origin, str):
            raise ValueError("explicit project control origin required")
        if not _TASK_ID.fullmatch(host_id) or not _TASK_ID.fullmatch(installation_id):
            raise ValueError("invalid selected host or installation identity")
        self.project = project
        self.host_id = host_id
        self.installation_id = installation_id
        self.origin = expected_origin
        self.branch = project.control_branch
        self.snapshot = f"refs/remotes/origin/{self.branch}"
        self.prefix = f".agent/hosts/{host_id}"
        self.control = project.control

    def _git(self, *args: str) -> bytes:
        # All calls below are local readonly: remote get-url, rev-parse,
        # ls-tree, cat-file. Never fetch, checkout, reset, commit or push.
        try:
            command = subprocess.run(
                ["git", "-C", str(self.control), *args],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
                timeout=15, check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ValueError("project snapshot Git operation unavailable") from exc
        if command.returncode:
            raise ValueError("project snapshot Git operation failed")
        return command.stdout

    def verify_snapshot(self) -> str:
        if (not self.control.is_absolute() or self.control.is_symlink()
                or not self.control.is_dir()
                or not (self.control / ".git").is_dir()):
            raise ValueError("explicit project control checkout required")
        actual_origin = self._git("remote", "get-url", "origin").decode("utf-8").strip()
        if actual_origin != self.origin:
            raise ValueError("project control origin differs from exact configuration")
        # Snapshot must exist. Git fetch freshness is an external obligation;
        # this method does not contact any remote or claim live Git readiness.
        sha = self._git("rev-parse", "--verify", self.snapshot + "^{commit}").decode().strip()
        if not re.fullmatch(r"[0-9a-f]{40,64}", sha):
            raise ValueError("invalid project snapshot commit")
        return sha

    def _blob(self, name: str, *, snapshot_sha: str | None = None) -> bytes | None:
        # Freeze every ls-tree lookup to the same object ID. Do not mix
        # tasks and results from a mutable remote-tracking branch ref.
        frozen = snapshot_sha or self.verify_snapshot()
        if not re.fullmatch(r"[0-9a-f]{40,64}", frozen):
            raise ValueError("invalid frozen project snapshot")
        tree = self._git("ls-tree", "-z", frozen, "--", name)
        if not tree:
            return None
        entries = tree.split(b"\x00")
        if len(entries) != 2 or entries[-1] != b"":
            raise ValueError("ambiguous Git project blob")
        metadata, sep, path = entries[0].partition(b"\t")
        fields = metadata.split(b" ")
        if (not sep or path.decode("utf-8", errors="replace") != name
                or len(fields) != 3 or fields[0] != b"100644"
                or fields[1] != b"blob"
                or not re.fullmatch(rb"[0-9a-f]{40,64}", fields[2])):
            raise ValueError("unsafe Git project task/result blob")
        size = int(self._git("cat-file", "-s", fields[2].decode()).strip())
        if not 0 <= size <= MAX_TASK_BYTES:
            raise ValueError("oversized Git project blob")
        blob = self._git("cat-file", "blob", fields[2].decode())
        if len(blob) != size:
            raise ValueError("Git project blob size mismatch")
        return blob

    def remote_result(self, task_id: str) -> bytes | None:
        if not _TASK_ID.fullmatch(task_id):
            raise ValueError("invalid task ID")
        frozen = self.verify_snapshot()
        return self._blob(f"{self.prefix}/results/{task_id}.json",
                          snapshot_sha=frozen)

    def pending_tasks(self, *, claimed: set[str] | None = None) -> list[ProjectCandidate]:
        """Return read-only candidates. No claim is made or file created."""
        frozen = self.verify_snapshot()
        prefix = f"{self.prefix}/tasks/"
        listed = self._git("ls-tree", "-r", "-z", "--name-only",
                           frozen, "--", prefix)
        candidates: list[ProjectCandidate] = []
        for raw_path in sorted(listed.split(b"\x00")):
            if not raw_path:
                continue
            try:
                path = raw_path.decode("utf-8", errors="strict")
            except UnicodeDecodeError:
                # Git permits arbitrary byte names; an unaddressable basename
                # must not suppress valid later tasks or create a local claim.
                continue
            if not path.startswith(prefix) or not path.endswith(".json"):
                continue
            task_id = path[len(prefix):-5]
            if not _TASK_ID.fullmatch(task_id):
                # An unaddressable Git basename is not an admitted task.
                # Ignore it before interpreting payload or writing a claim,
                # as with the shared host Git inbox. A valid basename with
                # malformed content/identity still fails closed below.
                continue
            if claimed is not None and task_id in claimed:
                continue
            if self._blob(f"{self.prefix}/results/{task_id}.json",
                          snapshot_sha=frozen) is not None:
                continue
            blob = self._blob(path, snapshot_sha=frozen)
            if blob is None:
                raise ValueError("project task disappeared from immutable snapshot")
            try:
                task = json.loads(blob, object_pairs_hook=_pairs)
            except (ValueError, UnicodeDecodeError) as exc:
                raise ValueError("invalid project task JSON") from exc
            if not isinstance(task, dict):
                raise ValueError("project task must be object")
            if task.get("id") != task_id or type(task.get("version")) is not int or task["version"] != 1:
                raise ValueError("project task identity/version mismatch")
            binding = canonical_agent_binding(task.get("agent_binding"))
            if binding != self.project.agent_binding:
                raise ValueError("project task binding mismatch")
            # The B2 task format may omit repository_id/installation_id.
            # When present these fields are never permitted to contradict
            # the selected local project and selected host installation.
            if task.get("repository_id", self.project.repository_id) != self.project.repository_id:
                raise ValueError("project task repository mismatch")
            if task.get("target_host_id", self.host_id) != self.host_id:
                raise ValueError("project task host mismatch")
            if task.get("target_installation_id", self.installation_id) != self.installation_id:
                raise ValueError("project task installation mismatch")
            candidates.append(ProjectCandidate(
                task_id, blob, hashlib.sha256(blob).hexdigest(), task, frozen))
        return candidates
