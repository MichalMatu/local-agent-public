"""Explicit project result-only Git publisher, isolated from B2/work checkouts.

No project task intake or execution. All bytes originate from previously
verified durable project spools, never from a command running here.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path

from linux_agent.foundation.process import atomic_write_text
from linux_agent.repository.journal import ProjectJournal
from linux_agent.repository.mailbox import MAX_TASK_BYTES, _TASK_ID

_OID = re.compile(rb"^[0-9a-f]{40,64}$")


class ProjectPublishError(RuntimeError):
    pass


class ProjectResultPublisher:
    """Own one *disposable* project Git control checkout under private state.

    Never reset or write the owner's selected project.control/work checkout.
    This object is not called by the running Linux Agent.
    """

    def __init__(self, journal: ProjectJournal, *, origin: str):
        if not isinstance(origin, str) or not origin:
            raise ValueError("exact project origin required")
        self.journal = journal
        self.project = journal.project
        self.origin = origin
        self.branch = self.project.control_branch
        self.root = journal.state / "project-transports" / self.project.repository_id
        self.checkout = self.root / "control"
        self.marker = self.root / "OWNED_BY_PROJECT_PUBLISHER.json"
        self.prefix = f".agent/hosts/{journal.host_id}/results/"

    def _git(self, *args: str, cwd: Path | None = None, timeout: int = 30) -> bytes:
        try:
            result = subprocess.run(
                ["git", *(["-C", str(cwd)] if cwd is not None else []), *args],
                capture_output=True, check=False, timeout=timeout,
                env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ProjectPublishError("project result Git operation unavailable") from exc
        if result.returncode:
            # Do not expose remote messages, tokens or private project paths.
            raise ProjectPublishError(f"project Git {args[0]} failed")
        return result.stdout

    def _private(self, path: Path, *, create: bool) -> None:
        if path.is_symlink():
            raise ValueError("project publisher storage must not be symlinked")
        if not path.exists():
            if not create:
                return
            path.mkdir(mode=0o700)
        if (not path.is_dir() or path.stat().st_uid != os.getuid()
                or path.stat().st_mode & 0o077):
            raise ValueError("project publisher storage must be owner-private")

    def initialize(self) -> None:
        self._private(self.journal.state, create=True)
        self._private(self.journal.state / "project-transports", create=True)
        self._private(self.root, create=True)
        expected = {
            "version": 1, "repository_id": self.project.repository_id,
            "origin": self.origin, "branch": self.branch,
            "checkout": str(self.checkout),
            "host_id": self.journal.host_id,
            "installation_id": self.journal.installation_id,
            "agent_binding": self.project.agent_binding,
        }
        if not self.checkout.exists():
            if self.marker.exists() or self.marker.is_symlink():
                raise ValueError("project transport marker exists without checkout")
            self._git("clone", "--branch", self.branch, "--single-branch",
                      self.origin, str(self.checkout), cwd=self.root, timeout=90)
            atomic_write_text(self.marker, json.dumps(expected, sort_keys=True) + "\n")
        if (self.checkout.is_symlink() or not self.checkout.is_dir()
                or (self.checkout / ".git").is_symlink()
                or not (self.checkout / ".git").is_dir()):
            raise ValueError("project transport requires owned dedicated Git checkout")
        if self.marker.is_symlink() or not self.marker.is_file():
            raise ValueError("unowned project Git transport checkout")
        try:
            marker = json.loads(self.marker.read_bytes())
        except (OSError, ValueError) as exc:
            raise ValueError("corrupt project transport ownership marker") from exc
        if marker != expected:
            raise ValueError("project transport origin/identity marker mismatch")
        actual = self._git("remote", "get-url", "origin", cwd=self.checkout).decode().strip()
        if actual != self.origin:
            raise ValueError("project result transport remote differs from selected origin")

    def _refresh(self) -> str:
        self.initialize()
        self._git("fetch", "--no-tags", "origin",
                  "+refs/heads/" + self.branch +
                  ":refs/remotes/origin/" + self.branch,
                  cwd=self.checkout, timeout=90)
        sha = self._git(
            "rev-parse", "--verify",
            f"refs/remotes/origin/{self.branch}^{{commit}}",
            cwd=self.checkout).decode().strip()
        if not _OID.fullmatch(sha.encode()):
            raise ValueError("invalid project remote revision")
        return sha

    def _remote_result(self, task_id: str, sha: str) -> bytes | None:
        if not _TASK_ID.fullmatch(task_id) or not _OID.fullmatch(sha.encode()):
            raise ValueError("invalid project result or snapshot identity")
        path = self.prefix + task_id + ".json"
        lines = self._git("ls-tree", "-z", sha, "--", path, cwd=self.checkout)
        if not lines:
            return None
        entries = lines.split(b"\x00")
        if len(entries) != 2 or entries[-1]:
            raise ValueError("ambiguous project result object")
        metadata, separator, name = entries[0].partition(b"\t")
        fields = metadata.split(b" ")
        if (not separator or name != path.encode() or len(fields) != 3
                or fields[0] != b"100644" or fields[1] != b"blob"
                or not _OID.fullmatch(fields[2])):
            raise ValueError("unsafe Git project result object")
        size = int(self._git("cat-file", "-s", fields[2].decode(), cwd=self.checkout))
        if not 0 <= size <= MAX_TASK_BYTES:
            raise ValueError("oversized remote project result")
        data = self._git("cat-file", "blob", fields[2].decode(), cwd=self.checkout)
        if len(data) != size:
            raise ValueError("Git project result size mismatch")
        return data

    def publish_result(self, task_id: str, data: bytes) -> None:
        """Publish only an immutable already-verified journal result.

        Remote conflicts and ambiguous push outcomes never edit local
        durable results or replay the project task.
        """
        if not _TASK_ID.fullmatch(task_id):
            raise ValueError("invalid project result task ID")
        if not isinstance(data, bytes) or len(data) > MAX_TASK_BYTES:
            raise ValueError("invalid bounded project result bytes")
        verified = dict(self.journal.verified_results())
        if verified.get(task_id) != data:
            raise ValueError("result bytes not verified by durable project journal")
        for _attempt in range(4):
            sha = self._refresh()
            current = self._remote_result(task_id, sha)
            if current is not None:
                if current == data:
                    return
                raise ProjectPublishError("immutable project result conflict")
            # This is explicitly OUR disposable checkout, with exact owner
            # marker/origin independently revalidated by initialize().
            self._git("reset", "--hard", sha, cwd=self.checkout)
            dest = self.checkout / self.prefix / (task_id + ".json")
            dest.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_text(dest, data.decode("utf-8"))
            self._git("add", "--", self.prefix + task_id + ".json", cwd=self.checkout)
            self._git("-c", "user.name=Linux Project Agent",
                      "-c", "user.email=linux-project-agent@local.invalid",
                      "commit", "-m", "local-agent: publish project result " + task_id,
                      cwd=self.checkout)
            try:
                self._git("push", "origin", "HEAD:refs/heads/" + self.branch,
                          cwd=self.checkout, timeout=90)
            except ProjectPublishError:
                # Push failure can mean remote success + lost ACK. Fetch
                # next and compare the immutable result; never rerun task.
                continue
            readback = self._refresh()
            if self._remote_result(task_id, readback) == data:
                return
        # One last independent remote readback after ambiguous pushes.
        latest = self._refresh()
        current = self._remote_result(task_id, latest)
        if current == data:
            return
        if current is not None:
            raise ProjectPublishError("immutable project result conflict")
        raise ProjectPublishError("unconfirmed project result publication; durable spool retained")

    def publish_pending(self) -> int:
        """Publish only already-spooled results; NEVER run a project task."""
        count = 0
        for task_id, data in self.journal.verified_results():
            self.publish_result(task_id, data)
            count += 1
        return count
