"""Build remote argv for deterministic Git workspace preparation and execution."""

from __future__ import annotations

from collections.abc import Sequence

from .models import RemoteGitWorkspace

READY_PREFIX = "__HOSTOPS_REMOTE_GIT_READY__"

_REMOTE_GIT_SCRIPT = r"""
set -euo pipefail
repo_url="$1"
revision="$2"
workspace="$3"
lock_name="$4"
clean_mode="$5"
shift 5

cache_home="${XDG_CACHE_HOME:-$HOME/.cache}"
hostops_root="$cache_home/host-ops"
root="$hostops_root/remote-git"
workspace_root="$root/workspaces"
workspace_dir="$workspace_root/$workspace"
repo_dir="$workspace_dir/repo"
repository_marker="$workspace_dir/repository-url"
lock_dir="$root/locks"

ensure_real_directory() {
  path="$1"
  label="$2"
  if [[ -L "$path" || ( -e "$path" && ! -d "$path" ) ]]; then
    echo "$label is not a real directory: $path" >&2
    exit 73
  fi
  if [[ ! -e "$path" ]]; then
    mkdir "$path"
  fi
  if [[ -L "$path" || ! -d "$path" ]]; then
    echo "$label could not be established safely: $path" >&2
    exit 73
  fi
}

mkdir -p "$cache_home"
ensure_real_directory "$hostops_root" "host-ops cache root"
ensure_real_directory "$root" "remote Git cache root"
ensure_real_directory "$workspace_root" "remote Git workspace root"
ensure_real_directory "$lock_dir" "remote Git lock directory"
if [[ -L "$workspace_dir" || ( -e "$workspace_dir" && ! -d "$workspace_dir" ) ]]; then
  echo "remote Git workspace path is not a real directory: $workspace" >&2
  exit 73
fi
if [[ ! -e "$workspace_dir" ]]; then
  mkdir "$workspace_dir"
fi
if [[ -L "$workspace_dir" || ! -d "$workspace_dir" ]]; then
  echo "remote Git workspace path could not be established safely: $workspace" >&2
  exit 73
fi

command -v git >/dev/null 2>&1 || { echo "remote git workflow requires git" >&2; exit 69; }
command -v flock >/dev/null 2>&1 || { echo "remote git workflow requires flock" >&2; exit 69; }

clean_workspace() {
  if [[ "$clean_mode" == "full" ]]; then
    git -C "$repo_dir" clean -ffdx
  else
    git -C "$repo_dir" clean -ffd
  fi
}

if [[ "$lock_name" != "-" ]]; then
  host_lock="$lock_dir/host-$lock_name.lock"
  if [[ -L "$host_lock" || ( -e "$host_lock" && ! -f "$host_lock" ) ]]; then
    echo "remote host lock path is not a regular file: $lock_name" >&2
    exit 73
  fi
  exec 9>>"$host_lock"
  if ! flock -n 9; then
    echo "remote host lock is busy: $lock_name" >&2
    exit 75
  fi
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

if [[ -e "$repository_marker" || -L "$repository_marker" ]]; then
  if [[ -L "$repository_marker" || ! -f "$repository_marker" ]]; then
    echo "remote Git repository marker is not a regular file: $workspace" >&2
    exit 73
  fi
  bound_repo_url="$(cat "$repository_marker")"
  if [[ -z "$bound_repo_url" ]]; then
    echo "remote Git repository marker is malformed: $workspace" >&2
    exit 73
  fi
  if [[ "$bound_repo_url" == *$'\t'* ||
        "$bound_repo_url" == *$'\r'* ||
        "$bound_repo_url" == *$'\n'* ]]; then
    echo "remote Git repository marker is malformed: $workspace" >&2
    exit 73
  fi
  if [[ "$bound_repo_url" != "$repo_url" ]]; then
    echo "remote workspace is bound to a different repository URL: $workspace" >&2
    exit 73
  fi
fi

if [[ -L "$repo_dir" || ( -e "$repo_dir" && ! -d "$repo_dir" ) ]]; then
  echo "remote workspace repository path is not a real directory: $repo_dir" >&2
  exit 73
fi
if [[ -d "$repo_dir" && ( -L "$repo_dir/.git" || ! -d "$repo_dir/.git" ) ]]; then
  echo "remote workspace path exists but is not a real Git checkout: $repo_dir" >&2
  exit 73
fi

# Adopt pre-binding workspaces only when their existing origin proves the same repository URL.
if [[ -d "$repo_dir/.git" && ! -e "$repository_marker" ]]; then
  existing_repo_url="$(git -C "$repo_dir" remote get-url origin 2>/dev/null || true)"
  if [[ "$existing_repo_url" != "$repo_url" ]]; then
    echo "legacy remote workspace origin does not match requested repository URL: $workspace" >&2
    exit 73
  fi
  printf '%s\n' "$repo_url" > "$repository_marker"
fi

if [[ ! -e "$repo_dir" ]]; then
  if ! git clone --no-checkout -- "$repo_url" "$repo_dir"; then
    rm -rf "$repo_dir"
    echo "remote Git clone failed; partial workspace removed" >&2
    exit 70
  fi
  if [[ -L "$repo_dir" || ! -d "$repo_dir/.git" || -L "$repo_dir/.git" ]]; then
    rm -rf "$repo_dir"
    echo "remote Git clone produced an unsafe workspace" >&2
    exit 70
  fi
  printf '%s\n' "$repo_url" > "$repository_marker"
fi

git -C "$repo_dir" remote set-url origin "$repo_url"
git -C "$repo_dir" fetch --prune --prune-tags --tags origin
if ! git -C "$repo_dir" cat-file -e "${revision}^{commit}" 2>/dev/null; then
  echo "remote revision was not fetched from origin: $revision" >&2
  exit 74
fi
reachable="$(
  git -C "$repo_dir" for-each-ref \
    --contains="$revision" \
    --format='%(refname)' \
    refs/remotes/origin/ refs/tags/
)"
if [[ -z "$reachable" ]]; then
  echo "remote revision is not reachable from fetched origin refs: $revision" >&2
  exit 74
fi

# A previous build may have modified tracked files or left an untracked nested Git repository.
# Normalize the old checkout before switching revisions so stale state cannot block checkout.
git -C "$repo_dir" reset --hard
clean_workspace

git -C "$repo_dir" checkout --detach "$revision"
git -C "$repo_dir" reset --hard "$revision"
clean_workspace

actual="$(git -C "$repo_dir" rev-parse HEAD)"
if [[ "$actual" != "$revision" ]]; then
  echo "remote revision mismatch: expected=$revision actual=$actual" >&2
  exit 74
fi

printf '__HOSTOPS_REMOTE_GIT_READY__ workspace=%s revision=%s\n' "$workspace" "$actual"
cd "$repo_dir"
if (($#)); then
  exec "$@"
fi
""".strip()


def build_prepare_argv(workspace: RemoteGitWorkspace) -> tuple[str, ...]:
    return _build_argv(workspace, ())


def build_run_argv(
    workspace: RemoteGitWorkspace,
    remote_argv: Sequence[str],
) -> tuple[str, ...]:
    if isinstance(remote_argv, str | bytes):
        raise ValueError("remote command must be an argument sequence")
    command = tuple(remote_argv)
    if not command or not isinstance(command[0], str) or not command[0]:
        raise ValueError("remote command must contain a non-empty argv[0]")
    if any(not isinstance(value, str) for value in command):
        raise ValueError("remote command arguments must be strings")
    if any("\x00" in value for value in command):
        raise ValueError("remote command arguments must not contain NUL bytes")
    return _build_argv(workspace, command)


def _build_argv(workspace: RemoteGitWorkspace, command: tuple[str, ...]) -> tuple[str, ...]:
    return (
        "bash",
        "-lc",
        _REMOTE_GIT_SCRIPT,
        "hostops-remote-git",
        workspace.repository_url,
        workspace.revision,
        workspace.workspace,
        workspace.lock or "-",
        workspace.clean_mode,
        *command,
    )
