"""Render a host-specific Linux service using the existing serial supervisor."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

from local_agent.config import MAX_STOP_GRACE_SECONDS, MEMORY_LIMITS
from local_agent.repository.context import load_repository_registry

SERVICE_NAME = "local-agent.service"
JOURNAL_NAMESPACE = "local-agent"
STOP_FINALIZATION_SECONDS = 60


def _quote(value: str | Path, *, command: bool = False) -> str:
    text = str(value)
    if not text or any(ord(char) < 32 or ord(char) == 127 for char in text):
        raise ValueError("systemd values must not contain control characters")
    if not command:
        if text != text.strip() or "\\" in text:
            raise ValueError("systemd directory paths cannot have edge whitespace or backslashes")
        return text.replace("%", "%%")
    text = text.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%")
    text = text.replace("$", "$$")
    return '"' + text + '"'


def _absolute(path: Path) -> Path:
    return path.expanduser().absolute()


def render_service(
    *,
    repo_root: Path,
    python: Path,
    registry: Path,
    user: str,
    host_id: str,
    stop_grace_seconds: int = 300,
    memory_max_mb: int = 128,
) -> str:
    from local_agent.repository.host_routing import canonical_host_id

    canonical_host_id(host_id)
    if not re.fullmatch(r"[a-z_][a-z0-9_-]{0,31}", user) or user == "root":
        raise ValueError("the service requires an explicit non-root Linux user")
    if type(stop_grace_seconds) is not int or not 1 <= stop_grace_seconds <= MAX_STOP_GRACE_SECONDS:
        raise ValueError(f"stop grace must be in range 1..{MAX_STOP_GRACE_SECONDS}")
    if type(memory_max_mb) is not int or not 64 <= memory_max_mb <= 16384:
        raise ValueError("service memory limit must be in range 64..16384 MiB")
    repo_root, python, registry = map(_absolute, (repo_root, python, registry))
    command = _quote(python, command=True)
    registry_arg = _quote(registry, command=True)
    preflight = (
        f"{command} -m local_agent.platform.linux_systemd check "
        f"--repo-root {_quote(repo_root, command=True)} "
        f"--registry {registry_arg} --host-id {host_id}"
    )
    return "\n".join([
        "[Unit]",
        f"Description=Local Agent on {host_id}",
        "Wants=network-online.target",
        "After=network-online.target",
        "StartLimitIntervalSec=300",
        "StartLimitBurst=5",
        "",
        "[Service]",
        "Type=simple",
        f"User={user}",
        f"WorkingDirectory={_quote(repo_root)}",
        "UMask=0077",
        "Environment=PYTHONUNBUFFERED=1",
        "Environment=PYTHONDONTWRITEBYTECODE=1",
        f"Environment=LOCAL_AGENT_STOP_GRACE_SECONDS={stop_grace_seconds}",
        "Environment=LOCAL_AGENT_MEMORY_LIMIT_MB_MIN=1",
        f"Environment=LOCAL_AGENT_MEMORY_LIMIT_MB_DEFAULT={memory_max_mb // 4}",
        f"Environment=LOCAL_AGENT_MEMORY_LIMIT_MB_MAX={memory_max_mb // 2}",
        f"ExecStartPre={preflight}",
        f"ExecStart={command} -m local_agent.supervisor.serial --registry {registry_arg}",
        "Restart=on-failure",
        "RestartSec=10",
        "SuccessExitStatus=130",
        "KillMode=mixed",
        "KillSignal=SIGTERM",
        f"TimeoutStopSec={stop_grace_seconds + STOP_FINALIZATION_SECONDS}",
        "SendSIGKILL=yes",
        "MemoryAccounting=yes",
        f"MemoryHigh={memory_max_mb * 3 // 4}M",
        f"MemoryMax={memory_max_mb}M",
        f"MemorySwapMax={memory_max_mb // 2}M",
        "OOMPolicy=stop",
        "TasksMax=64",
        "Nice=10",
        "StandardOutput=journal",
        "StandardError=journal",
        f"LogNamespace={JOURNAL_NAMESPACE}",
        "LogRateLimitIntervalSec=30s",
        "LogRateLimitBurst=200",
        "",
        "[Install]",
        "WantedBy=multi-user.target",
        "",
    ])


def render_journal_config() -> str:
    """Bound this service's logs independently of the system journal."""
    return (
        "[Journal]\nStorage=persistent\nSystemMaxUse=16M\nRuntimeMaxUse=8M\n"
        "SystemMaxFileSize=4M\nRuntimeMaxFileSize=2M\nMaxRetentionSec=7day\n"
    )


def check_installation(*, repo_root: Path, registry: Path, host_id: str) -> dict[str, object]:
    repo_root, registry = map(_absolute, (repo_root, registry))
    if registry.is_symlink() or not registry.is_file():
        raise ValueError("an explicit regular host registry is required")
    required = ("agent_multirepo.py", "local_agent/supervisor/serial.py", "local_agent/supervisor/worker.py")
    if any(not (repo_root / name).is_file() for name in required):
        raise ValueError("the selected checkout is missing packaged supervisor/worker files")
    # The service and its installed preflight must select the same optional
    # host-owned control source. Empty project lists are valid only for an
    # explicitly enabled, privately identified standalone installation.
    from local_agent.daemon.service import STATE_DIR
    from local_agent.supervisor.host_control import load_host_control_source

    host_control = load_host_control_source(STATE_DIR / "supervisor-control.json")
    if host_control is not None and host_control.host_id != host_id:
        raise ValueError("host supervisor control targets a different host")
    repositories = load_repository_registry(
        path=registry, allow_empty=host_control is not None,
    )
    if (not repositories and host_control is None) or any(
        repo.host_id != host_id or not repo.host_mailbox_enabled
        or not repo.host_execution_enabled or repo.agent_binding is None
        for repo in repositories
    ):
        raise ValueError("every enabled repository must be bound to the selected host mailbox")
    return {
        "status": "READY", "scope": "service_configuration",
        "host_id": host_id, "repository_count": len(repositories),
        "execution_authority": "not_asserted",
        "memory_limit_mb_default": MEMORY_LIMITS.default_mb,
        "memory_limit_mb_max": MEMORY_LIMITS.maximum_mb,
        "memory_controller_available": _memory_controller_available(),
    }


def _memory_controller_available() -> bool | None:
    if sys.platform != "linux":
        return None
    try:
        return "memory" in Path("/sys/fs/cgroup/cgroup.controllers").read_text().split()
    except OSError:
        return False


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    actions = parser.add_subparsers(dest="action", required=True)
    for name in ("render", "check"):
        action = actions.add_parser(name)
        action.add_argument("--repo-root", type=Path, required=True)
        action.add_argument("--registry", type=Path, required=True)
        action.add_argument("--host-id", required=True)
        if name == "render":
            action.add_argument("--python", type=Path, required=True)
            action.add_argument("--user", required=True)
            action.add_argument("--stop-grace-seconds", type=int, default=300)
            action.add_argument("--memory-max-mb", type=int, default=128)
    actions.add_parser("journal-config")
    args = parser.parse_args()
    try:
        if args.action == "journal-config":
            print(render_journal_config(), end="")
        elif args.action == "render":
            print(render_service(
                repo_root=args.repo_root, python=args.python, registry=args.registry,
                user=args.user, host_id=args.host_id,
                stop_grace_seconds=args.stop_grace_seconds, memory_max_mb=args.memory_max_mb,
            ), end="")
        else:
            print(json.dumps(check_installation(
                repo_root=args.repo_root, registry=args.registry, host_id=args.host_id,
            ), sort_keys=True))
    except (ValueError, OSError) as exc:
        print(f"Local Agent systemd configuration rejected: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
