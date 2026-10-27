"""Explicit inspection and removal of deterministic remote Git cache workspaces."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from local_agent.host_ops.capabilities.remote.ssh import SshClient
from local_agent.host_ops.core.config import HostTarget
from local_agent.host_ops.core.execution import ExecutionLimits, ProcessResult

from .models import validate_workspace_name

_CACHE_LIST_SCRIPT = r"""
set -euo pipefail
cache_home="${XDG_CACHE_HOME:-$HOME/.cache}"
hostops_root="$cache_home/host-ops"
root="$hostops_root/remote-git"
workspace_root="$root/workspaces"
lock_dir="$root/locks"

if [[ -L "$hostops_root" || ( -e "$hostops_root" && ! -d "$hostops_root" ) ]]; then
  echo "host-ops cache root is not a real directory" >&2
  exit 73
fi
if [[ -L "$root" || ( -e "$root" && ! -d "$root" ) ]]; then
  echo "remote Git cache root is not a real directory" >&2
  exit 73
fi
if [[ -L "$lock_dir" || ( -e "$lock_dir" && ! -d "$lock_dir" ) ]]; then
  echo "remote Git lock directory is not a real directory" >&2
  exit 73
fi
mkdir -p "$lock_dir"
if [[ -L "$lock_dir" || ! -d "$lock_dir" ]]; then
  echo "remote Git lock directory could not be established safely" >&2
  exit 73
fi

if [[ ! -e "$workspace_root" && ! -L "$workspace_root" ]]; then
  exit 0
fi
if [[ -L "$workspace_root" || ! -d "$workspace_root" ]]; then
  echo "remote Git workspace root is not a real directory" >&2
  exit 73
fi

shopt -s nullglob
for workspace_dir in "$workspace_root"/*; do
  workspace="${workspace_dir##*/}"
  if [[ ! "$workspace" =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$ ]]; then
    echo "remote Git cache contains an invalid workspace name: $workspace" >&2
    exit 73
  fi
  if [[ -L "$workspace_dir" || ! -d "$workspace_dir" ]]; then
    echo "remote Git workspace path is not a real directory: $workspace" >&2
    exit 73
  fi

  workspace_lock="$lock_dir/workspace-$workspace.lock"
  if [[ -L "$workspace_lock" || ( -e "$workspace_lock" && ! -f "$workspace_lock" ) ]]; then
    echo "remote workspace lock path is not a regular file: $workspace" >&2
    exit 73
  fi
  exec 8>>"$workspace_lock"
  if ! flock -n 8; then
    printf '%s\tbusy\t\t\n' "$workspace"
    exec 8>&-
    continue
  fi

  repository_marker="$workspace_dir/repository-url"
  repo_dir="$workspace_dir/repo"
  repository_url=""
  revision=""
  state="empty"

  if [[ -e "$repository_marker" || -L "$repository_marker" ]]; then
    if [[ -L "$repository_marker" || ! -f "$repository_marker" ]]; then
      echo "remote Git repository marker is not a regular file: $workspace" >&2
      exit 73
    fi
    repository_url="$(cat "$repository_marker")"
    if [[ -z "$repository_url" ]] ||
       [[ "$repository_url" == *$'\t'* ]] ||
       [[ "$repository_url" == *$'\r'* ]] ||
       [[ "$repository_url" == *$'\n'* ]]; then
      echo "remote Git repository marker is malformed: $workspace" >&2
      exit 73
    fi
    state="bound-empty"
  fi

  if [[ -e "$repo_dir" || -L "$repo_dir" ]]; then
    if [[ -L "$repo_dir" || -L "$repo_dir/.git" || ! -d "$repo_dir/.git" ]]; then
      echo "remote Git cache repository is invalid: $workspace" >&2
      exit 73
    fi
    if ! command -v git >/dev/null 2>&1; then
      echo "remote git cache inspection requires git" >&2
      exit 69
    fi
    revision="$(git -C "$repo_dir" rev-parse HEAD 2>/dev/null || true)"
    if [[ ! "$revision" =~ ^([0-9a-f]{40}|[0-9a-f]{64})$ ]]; then
      echo "remote Git cache repository HEAD is invalid: $workspace" >&2
      exit 73
    fi
    if [[ -n "$repository_url" ]]; then
      state="ready"
    else
      state="legacy-unbound"
    fi
  fi

  printf '%s\t%s\t%s\t%s\n' "$workspace" "$state" "$repository_url" "$revision"
  exec 8>&-
done
""".strip()

_CACHE_REMOVE_SCRIPT = r"""
set -euo pipefail
workspace="$1"
cache_home="${XDG_CACHE_HOME:-$HOME/.cache}"
hostops_root="$cache_home/host-ops"
root="$hostops_root/remote-git"
workspace_root="$root/workspaces"
workspace_dir="$workspace_root/$workspace"
lock_dir="$root/locks"

if [[ -L "$hostops_root" || ( -e "$hostops_root" && ! -d "$hostops_root" ) ]]; then
  echo "host-ops cache root is not a real directory" >&2
  exit 73
fi
if [[ -L "$root" || ( -e "$root" && ! -d "$root" ) ]]; then
  echo "remote Git cache root is not a real directory" >&2
  exit 73
fi
if [[ -L "$lock_dir" || ( -e "$lock_dir" && ! -d "$lock_dir" ) ]]; then
  echo "remote Git lock directory is not a real directory" >&2
  exit 73
fi
mkdir -p "$lock_dir"
if [[ -L "$lock_dir" || ! -d "$lock_dir" ]]; then
  echo "remote Git lock directory could not be established safely" >&2
  exit 73
fi

if [[ -L "$workspace_root" || ( -e "$workspace_root" && ! -d "$workspace_root" ) ]]; then
  echo "remote Git workspace root is not a real directory" >&2
  exit 73
fi

workspace_lock="$lock_dir/workspace-$workspace.lock"
if [[ -L "$workspace_lock" || ( -e "$workspace_lock" && ! -f "$workspace_lock" ) ]]; then
  echo "remote workspace lock path is not a regular file: $workspace" >&2
  exit 73
fi
exec 8>>"$workspace_lock"
if ! flock -n 8; then
  echo "remote workspace is busy: $workspace" >&2
  exit 75
fi

if [[ ! -e "$workspace_dir" && ! -L "$workspace_dir" ]]; then
  printf '__HOSTOPS_REMOTE_GIT_CACHE_REMOVED__ workspace=%s existed=0\n' "$workspace"
  exit 0
fi
if [[ -L "$workspace_dir" || ! -d "$workspace_dir" ]]; then
  echo "remote Git workspace path is not a real directory: $workspace" >&2
  exit 73
fi

rm -rf -- "$workspace_dir"
if [[ -e "$workspace_dir" ]]; then
  echo "remote Git workspace removal did not complete: $workspace" >&2
  exit 70
fi
printf '__HOSTOPS_REMOTE_GIT_CACHE_REMOVED__ workspace=%s existed=1\n' "$workspace"
""".strip()

_REMOVE_PREFIX = "__HOSTOPS_REMOTE_GIT_CACHE_REMOVED__"


class RemoteGitCacheError(RuntimeError):
    """Raised when remote cache evidence is malformed or incomplete."""


@dataclass(frozen=True, slots=True)
class RemoteGitCacheEntry:
    workspace: str
    state: str
    repository_url: str | None = None
    revision: str | None = None

    @property
    def busy(self) -> bool:
        return self.state == "busy"

    def as_dict(self) -> dict[str, Any]:
        return {
            "workspace": self.workspace,
            "state": self.state,
            "busy": self.busy,
            "repository_url": self.repository_url,
            "revision": self.revision,
        }


@dataclass(frozen=True, slots=True)
class RemoteGitCacheListResult:
    entries: tuple[RemoteGitCacheEntry, ...]
    process: ProcessResult

    @property
    def ok(self) -> bool:
        return self.process.ok

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "entries": [entry.as_dict() for entry in self.entries],
            "process": self.process.as_dict(),
        }


@dataclass(frozen=True, slots=True)
class RemoteGitCacheRemoveResult:
    workspace: str
    existed: bool
    process: ProcessResult

    @property
    def ok(self) -> bool:
        return self.process.ok

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "workspace": self.workspace,
            "existed": self.existed,
            "process": self.process.as_dict(),
        }


class RemoteGitCacheManager:
    """Inspect or remove remote Git cache state through hardened SSH."""

    def __init__(self, client: SshClient | None = None) -> None:
        self._client = client or SshClient()

    def list(
        self,
        target: HostTarget,
        *,
        limits: ExecutionLimits | None = None,
    ) -> RemoteGitCacheListResult:
        process = self._client.execute(target, build_cache_list_argv(), limits=limits)
        if not process.ok:
            return RemoteGitCacheListResult(entries=(), process=process)
        if process.stdout_truncated:
            raise RemoteGitCacheError("remote cache inventory output was truncated")
        return RemoteGitCacheListResult(
            entries=_parse_cache_entries(process.stdout), process=process
        )

    def remove(
        self,
        target: HostTarget,
        workspace: str,
        *,
        limits: ExecutionLimits | None = None,
    ) -> RemoteGitCacheRemoveResult:
        normalized_workspace = validate_workspace_name(workspace)
        process = self._client.execute(
            target,
            build_cache_remove_argv(normalized_workspace),
            limits=limits,
        )
        if not process.ok:
            return RemoteGitCacheRemoveResult(
                workspace=normalized_workspace,
                existed=False,
                process=process,
            )
        if process.stdout_truncated:
            raise RemoteGitCacheError("remote cache removal evidence was truncated")
        existed = _parse_remove_evidence(process.stdout, normalized_workspace)
        return RemoteGitCacheRemoveResult(
            workspace=normalized_workspace,
            existed=existed,
            process=process,
        )


def build_cache_list_argv() -> tuple[str, ...]:
    return ("bash", "-lc", _CACHE_LIST_SCRIPT)


def build_cache_remove_argv(workspace: str) -> tuple[str, ...]:
    normalized = validate_workspace_name(workspace)
    return ("bash", "-lc", _CACHE_REMOVE_SCRIPT, "hostops-remote-git-cache-remove", normalized)


def _parse_cache_entries(stdout: str) -> tuple[RemoteGitCacheEntry, ...]:
    entries: list[RemoteGitCacheEntry] = []
    seen: set[str] = set()
    for line in stdout.splitlines():
        parts = line.split("\t")
        if len(parts) != 4:
            raise RemoteGitCacheError("remote cache inventory returned malformed evidence")
        workspace, state, repository_url, revision = parts
        try:
            validate_workspace_name(workspace)
        except ValueError as exc:
            raise RemoteGitCacheError("remote cache inventory returned invalid workspace") from exc
        if workspace in seen:
            raise RemoteGitCacheError("remote cache inventory duplicated a workspace")
        seen.add(workspace)
        if state not in {"empty", "bound-empty", "ready", "legacy-unbound", "busy"}:
            raise RemoteGitCacheError(f"remote cache inventory returned unknown state: {state}")
        if revision and not (
            len(revision) in {40, 64}
            and revision == revision.lower()
            and all(char in "0123456789abcdef" for char in revision)
        ):
            raise RemoteGitCacheError("remote cache inventory returned invalid revision")
        entries.append(
            RemoteGitCacheEntry(
                workspace=workspace,
                state=state,
                repository_url=repository_url or None,
                revision=revision or None,
            )
        )
    return tuple(entries)


def _parse_remove_evidence(stdout: str, workspace: str) -> bool:
    lines = [line.strip() for line in stdout.splitlines() if line.strip()]
    if len(lines) != 1 or not lines[0].startswith(f"{_REMOVE_PREFIX} "):
        raise RemoteGitCacheError("remote cache removal returned malformed success evidence")
    fields = dict(field.split("=", 1) for field in lines[0].split()[1:] if "=" in field)
    if fields.get("workspace") != workspace or fields.get("existed") not in {"0", "1"}:
        raise RemoteGitCacheError("remote cache removal returned malformed success evidence")
    return fields["existed"] == "1"
