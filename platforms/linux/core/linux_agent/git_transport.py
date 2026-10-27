"""Git v1 host task/result transport; the dedicated control clone is disposable."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
from typing import Any

from linux_agent.foundation.process import atomic_write_text

BRANCH = "agent-control"
TASK_ID_CHARS = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-"
)


def valid_task_id(value: str) -> bool:
    """Git inbox names must be safe for immutable claims, spools and results."""
    return (isinstance(value, str) and 1 <= len(value) <= 200
            and all(char in TASK_ID_CHARS for char in value))


class GitError(RuntimeError):
    pass


class GitTransport:
    def __init__(self, config: dict[str, Any], state: Path):
        self.config = config
        self.state = state
        self.checkout = state / "transport" / "agent-control"
        self.marker = state / "transport" / "OWNED_BY_LINUX_AGENT.json"
        self.host = config["host_id"]
        self.installation = config["installation_id"]
        self.prefix = f".agent/hosts/{self.host}/supervisor/{self.installation}"
        self.remote = config["repository"]

    def git(self, *args: str, cwd: Path | None = None, timeout: int = 30) -> str:
        env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
        if self.config.get("ssh_command"):
            env["GIT_SSH_COMMAND"] = self.config["ssh_command"]
        cmd = ["git", *(["-C", str(cwd)] if cwd else []), *args]
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, env=env,
                                    timeout=timeout, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise GitError(f"Git operation failed or timed out: {args[0]}") from exc
        if result.returncode:
            # Never include arbitrary remote stderr (could contain secrets or task paths).
            raise GitError(f"Git {args[0]} failed with exit {result.returncode}")
        return result.stdout

    def initialize(self) -> None:
        if not self.checkout.exists():
            if self.marker.exists():
                raise GitError("control marker present but checkout missing; inspect manually")
            self.checkout.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            self.git("clone", "--branch", BRANCH, "--single-branch", self.remote,
                     str(self.checkout), cwd=self.checkout.parent, timeout=90)
            atomic_write_text(self.marker, json.dumps(
                {"version": 1, "repository": self.remote, "branch": BRANCH,
                 "checkout": str(self.checkout.resolve())}) + "\n")
        if not self.checkout.is_dir() or self.checkout.is_symlink() or not (self.checkout / ".git").is_dir():
            raise GitError("invalid control checkout")
        if not self.marker.is_file() or self.marker.is_symlink():
            raise GitError("unowned control checkout; refusing to reset or reuse")
        try:
            marker = json.loads(self.marker.read_text())
        except (OSError, ValueError) as exc:
            raise GitError("corrupt control marker") from exc
        if marker != {"version": 1, "repository": self.remote, "branch": BRANCH,
                       "checkout": str(self.checkout.resolve())}:
            raise GitError("control origin/identity marker mismatch")
        if self.git("remote", "get-url", "origin", cwd=self.checkout).strip() != self.remote:
            raise GitError("control origin mismatch")

    def refresh(self) -> None:
        self.initialize()
        # The selected remote branch may be rewritten by an authorized
        # publisher. Refresh ONLY the disposable local remote-tracking ref;
        # never force-push or discard independent durable claims/results.
        # The owner-private marker and exact origin have been verified first.
        self.git("fetch", "--no-tags", "origin", "+refs/heads/" + BRANCH +
                 ":refs/remotes/origin/" + BRANCH, cwd=self.checkout, timeout=90)

    def _blob(self, path: str) -> bytes | None:
        mode = self.git("ls-tree", "origin/" + BRANCH, "--", path, cwd=self.checkout)
        if not mode.strip():
            return None
        if not mode.startswith("100644 blob ") or "\n" in mode.strip("\n"):
            raise GitError("unsafe or ambiguous transport blob mode")
        # Blob size is bounded before content enters JSON decoder.
        objid = mode.split()[2]
        size = int(self.git("cat-file", "-s", objid, cwd=self.checkout).strip())
        if size > 4 * 1024 * 1024:
            raise GitError("oversized Git task/result blob")
        try:
            r = subprocess.run(["git", "-C", str(self.checkout), "cat-file", "blob", objid],
                               stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                               check=True, timeout=20)
        except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
            raise GitError("cannot read transport blob") from exc
        return r.stdout

    def pending_task(self, task_id: str) -> bytes | None:
        """Read only the selected task, rejecting already published results."""
        if not valid_task_id(task_id):
            raise GitError("unsafe task id")
        if self._blob(f"{self.prefix}/task-results/{task_id}.json") is not None:
            return None
        return self._blob(f"{self.prefix}/tasks/{task_id}.json")

    def task_candidates(self, *, skip_ids: set[str] | None = None) -> list[tuple[str, bytes]]:
        root = self.prefix + "/tasks/"
        names = self.git("ls-tree", "-r", "--name-only", "origin/" + BRANCH,
                         "--", root, cwd=self.checkout)
        found = []
        for name in sorted(names.splitlines()):
            if not name.startswith(root) or not name.endswith(".json"):
                continue
            task_id = name[len(root):-5]
            if not valid_task_id(task_id):
                continue
            # Claimed tasks have durable local authority; they can never be
            # re-executed, even after remote Git history rewrite.
            if skip_ids is not None and task_id in skip_ids:
                continue
            raw = self.pending_task(task_id)
            if raw is not None:
                found.append((task_id, raw))
        return found

    def remote_result(self, task_id: str) -> bytes | None:
        """Read a result from the already refreshed remote-tracking tree."""
        if not valid_task_id(task_id):
            raise GitError("unsafe result id")
        return self._blob(f"{self.prefix}/task-results/{task_id}.json")

    def publish(self, task_id: str, data: bytes) -> None:
        path = f"{self.prefix}/task-results/{task_id}.json"
        if not valid_task_id(task_id):
            raise GitError("unsafe result id")
        for _attempt in range(4):
            self.refresh()
            existing = self._blob(path)
            if existing is not None:
                if existing == data:
                    return
                raise GitError("immutable remote result conflict")
            # Safe to reset: clone is marked dedicated disposable Linux Agent transport,
            # and durable result is stored outside it before this method is called.
            self.git("reset", "--hard", "origin/" + BRANCH, cwd=self.checkout)
            dest = self.checkout / path
            dest.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_text(dest, data.decode("utf-8"))
            self.git("add", "--", path, cwd=self.checkout)
            self.git("-c", "user.name=Debian Linux Agent", "-c",
                     "user.email=linux-agent@local.invalid",
                     "commit", "-m", "linux-agent: publish " + task_id,
                     cwd=self.checkout)
            try:
                self.git("push", "origin", "HEAD:refs/heads/" + BRANCH,
                         cwd=self.checkout, timeout=90)
            except GitError:
                # An ambiguous push must be resolved by reading origin on retry.
                continue
            self.refresh()
            if self._blob(path) == data:
                return
        raise GitError("result publication unconfirmed; durable spool retained")
