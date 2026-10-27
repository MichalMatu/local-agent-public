"""Isolated durable project claim/spool foundation, with no executor or publisher.

An active project execution process MUST own a task lease before marking
any claim executable. Crash recovery requires a caller-supplied successful
fencing callback; absent that evidence, UNKNOWN recovery fails closed.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any, Callable

from linux_agent.foundation.process import atomic_write_text, fsync_directory
from linux_agent.repository.projects import Project

_ID = re.compile(r"^[A-Za-z0-9._-]{1,200}$")
_DIGEST = re.compile(r"^[0-9a-f]{64}$")


def _pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate journal field")
        result[key] = value
    return result


class ProjectJournal:
    """A project-specific, owner-private claim/result store; never run tasks."""

    def __init__(
        self, state: Path, project: Project, *, host_id: str, installation_id: str
    ):
        if not state.is_absolute() or not _ID.fullmatch(project.repository_id):
            raise ValueError("absolute state and canonical project id required")
        if (host_id != project.host_id or not _ID.fullmatch(host_id)
                or not _ID.fullmatch(installation_id)
                or not project.host_mailbox_enabled or not project.host_execution_enabled
                or project.agent_binding is None):
            raise ValueError("selected project host/binding is not enabled")
        self.state = state
        self.project = project
        self.host_id = host_id
        self.installation_id = installation_id
        self.root = state / "projects" / project.repository_id

    def _private_dir(self, directory: Path, *, create: bool) -> None:
        for path in (self.state, self.state / "projects", self.root):
            if path.is_symlink():
                raise ValueError("project journal path must not be symlinked")
            if not path.exists():
                if create:
                    path.mkdir(mode=0o700)
                else:
                    continue
            if not path.is_dir() or path.stat().st_uid != os.getuid() or path.stat().st_mode & 0o077:
                raise ValueError("project journal must be owner-private directory")
        if directory in (self.root / "claims", self.root / "results"):
            if directory.is_symlink():
                raise ValueError("project journal subdirectory must not be symlinked")
            if create and not directory.exists():
                directory.mkdir(mode=0o700)
            if directory.exists() and (not directory.is_dir() or directory.stat().st_uid != os.getuid()
                                       or directory.stat().st_mode & 0o077):
                raise ValueError("project journal subdirectory must be owner-private")

    def _claim_path(self, task_id: str) -> Path:
        if not _ID.fullmatch(task_id):
            raise ValueError("invalid project task ID")
        return self.root / "claims" / f"{task_id}.json"

    def _result_path(self, task_id: str) -> Path:
        if not _ID.fullmatch(task_id):
            raise ValueError("invalid project result ID")
        return self.root / "results" / f"{task_id}.json"

    def claim(self, task_id: str, digest: str) -> None:
        """Exclusive fsynced pre-effect claim; caller must own a task lease."""
        if not _DIGEST.fullmatch(digest):
            raise ValueError("invalid exact project task digest")
        claim = self._claim_path(task_id)
        self._private_dir(claim.parent, create=True)
        data = {
            "version": 1, "id": task_id, "task_digest": digest,
            "status": "execution_may_have_started",
            "repository_id": self.project.repository_id,
            "agent_binding": self.project.agent_binding,
            "target_host_id": self.host_id,
            "target_installation_id": self.installation_id,
        }
        flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(claim, flags, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as out:
            out.write(json.dumps(data, sort_keys=True, separators=(",", ":")) + "\n")
            out.flush()
            os.fsync(out.fileno())
        fsync_directory(claim.parent)

    def _read_claim(self, task_id: str) -> dict[str, Any]:
        path = self._claim_path(task_id)
        self._private_dir(path.parent, create=False)
        if path.is_symlink() or not path.is_file():
            raise ValueError("project claim missing or unsafe")
        if path.stat().st_size > 4096:
            raise ValueError("oversized project claim")
        try:
            value = json.loads(path.read_bytes(), object_pairs_hook=_pairs)
        except (OSError, ValueError, UnicodeDecodeError) as exc:
            raise ValueError("corrupt project claim") from exc
        expected = {
            "version": 1, "id": task_id, "status": "execution_may_have_started",
            "repository_id": self.project.repository_id,
            "agent_binding": self.project.agent_binding,
            "target_host_id": self.host_id,
            "target_installation_id": self.installation_id,
        }
        if (not isinstance(value, dict) or set(value) != set(expected) | {"task_digest"}
                or any(value.get(k) != v for k, v in expected.items())
                or not isinstance(value.get("task_digest"), str)
                or not _DIGEST.fullmatch(value["task_digest"])):
            raise ValueError("project claim identity changed")
        return value

    def record_result(self, task_id: str, digest: str, result: dict[str, Any]) -> bytes:
        """Immutable terminal envelope, requires matching fsynced pre-effect claim."""
        claim = self._read_claim(task_id)
        if digest != claim["task_digest"]:
            raise ValueError("project result does not match claim digest")
        if not isinstance(result, dict) or result.get("status") not in {"done", "failed", "unknown"}:
            raise ValueError("project terminal result must be explicit")
        for name, expected in (
            ("id", task_id), ("task_digest", digest),
            ("repository_id", self.project.repository_id),
            ("agent_binding", self.project.agent_binding),
            ("target_host_id", self.host_id),
            ("target_installation_id", self.installation_id),
        ):
            if name in result and result[name] != expected:
                raise ValueError("nested project result identity conflict")
        envelope = {
            "version": 1, "id": task_id, "status": result["status"],
            "repository_id": self.project.repository_id,
            "agent_binding": self.project.agent_binding,
            "target_host_id": self.host_id,
            "target_installation_id": self.installation_id,
            "task_digest": digest, "result": result,
        }
        encoded = (json.dumps(envelope, sort_keys=True, ensure_ascii=False,
                              separators=(",", ":")) + "\n").encode("utf-8")
        if len(encoded) > 1024 * 1024:
            raise ValueError("oversized project result")
        path = self._result_path(task_id)
        self._private_dir(path.parent, create=True)
        if path.is_symlink():
            raise ValueError("project result spool must not be symlinked")
        if path.exists():
            if path.read_bytes() != encoded:
                raise ValueError("immutable project result conflict")
        else:
            atomic_write_text(path, encoded.decode("utf-8"))
        return encoded

    def verified_results(self) -> list[tuple[str, bytes]]:
        """Read/verify durable results without publishing them to a Git remote."""
        directory = self.root / "results"
        self._private_dir(directory, create=False)
        if not directory.exists():
            return []
        found = []
        for path in sorted(directory.glob("*.json")):
            if path.is_symlink() or not _ID.fullmatch(path.stem) or not path.is_file():
                raise ValueError("unsafe project result spool")
            blob = path.read_bytes()
            if len(blob) > 1024 * 1024:
                raise ValueError("oversized project result spool")
            try:
                item = json.loads(blob, object_pairs_hook=_pairs)
            except (ValueError, UnicodeDecodeError) as exc:
                raise ValueError("corrupt project result spool") from exc
            if not isinstance(item, dict) or set(item) != {
                "version", "id", "status", "repository_id", "agent_binding",
                "target_host_id", "target_installation_id", "task_digest", "result"
            }:
                raise ValueError("invalid project result envelope")
            claim = self._read_claim(path.stem)
            if (item["version"] != 1 or item["id"] != path.stem
                    or item["task_digest"] != claim["task_digest"]
                    or item["repository_id"] != self.project.repository_id
                    or item["agent_binding"] != self.project.agent_binding
                    or item["target_host_id"] != self.host_id
                    or item["target_installation_id"] != self.installation_id
                    or item["status"] not in {"done", "failed", "unknown"}
                    or not isinstance(item["result"], dict)
                    or item["result"].get("status") != item["status"]):
                raise ValueError("invalid project result claim/identity")
            for name, expected in (
                ("id", path.stem),
                ("task_digest", claim["task_digest"]),
                ("repository_id", self.project.repository_id),
                ("agent_binding", self.project.agent_binding),
                ("target_host_id", self.host_id),
                ("target_installation_id", self.installation_id),
            ):
                if name in item["result"] and item["result"][name] != expected:
                    raise ValueError("nested project result identity conflict")
            found.append((path.stem, blob))
        return found

    def recover_unknown(self, *, fence: Callable[[str], None] | None = None) -> int:
        """Fail closed without a proven task/process resource lease fence.

        A future worker must fence *surviving descendants* before it may
        publish UNKNOWN. Merely restarting Python does not prove they ended.
        """
        if fence is None:
            raise ValueError("project crash recovery requires independent lease fence")
        directory = self.root / "claims"
        self._private_dir(directory, create=False)
        if not directory.exists():
            return 0
        recovered = 0
        for path in sorted(directory.glob("*.json")):
            if path.is_symlink() or not _ID.fullmatch(path.stem):
                raise ValueError("unsafe project claim path")
            claim = self._read_claim(path.stem)
            result = self._result_path(path.stem)
            if result.is_symlink():
                raise ValueError("unsafe project result spool path")
            if result.exists():
                # Never reinterpret an existing terminal result.
                continue
            fence(path.stem)
            self.record_result(path.stem, claim["task_digest"], {
                "status": "unknown",
                "reason": "interrupted_or_uncertain_project_execution_no_replay",
            })
            recovered += 1
        return recovered
