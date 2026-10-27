"""Generate and manage the macOS LaunchAgent definition for Local Agent."""

from __future__ import annotations

import os
import plistlib
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Literal

from local_agent.supervisor.scheduling import MAX_MAX_WORKERS

LABEL = "com.michal.local-agent"
Mode = Literal["parallel", "multirepo", "single"]
# launchd must outlive the guarded entrypoint's bounded child cleanup. The
# supervisor may itself spend up to five seconds draining worker process groups
# before the entrypoint can finish reaping it, so the historical five-second
# launchd exit timeout could kill the guard just before its SIGKILL fallback.
LAUNCHD_EXIT_TIMEOUT_SECONDS = 15
UNLOAD_TIMEOUT_SECONDS = 20.0
UNLOAD_POLL_SECONDS = 0.1
BOOTSTRAP_RETRY_ATTEMPTS = 3
BOOTSTRAP_RETRY_DELAY_SECONDS = 0.5


def default_registry_path(home: Path) -> Path:
    return home / "Library" / "Application Support" / "local-agent" / "repositories.json"


def default_launch_agent_path(home: Path) -> Path:
    return home / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def default_path(home: Path) -> str:
    return ":".join(
        (
            str(home / ".platformio" / "penv" / "bin"),
            "/opt/homebrew/bin",
            "/usr/local/bin",
            "/usr/bin",
            "/bin",
            "/usr/sbin",
            "/sbin",
        )
    )


def build_program_arguments(
    mode: Mode,
    *,
    repo_root: Path,
    home: Path,
    max_workers: int = 2,
    registry_path: Path | None = None,
) -> list[str]:
    python = repo_root / ".venv" / "bin" / "python"
    if mode == "parallel":
        if max_workers < 1 or max_workers > MAX_MAX_WORKERS:
            raise ValueError(f"max_workers must be in range 1..{MAX_MAX_WORKERS}")
        registry = registry_path or default_registry_path(home)
        return [
            str(python),
            str(repo_root / "agent_entrypoint.py"),
            "--registry",
            str(registry),
            "--max-workers",
            str(max_workers),
        ]
    if mode == "multirepo":
        return [str(python), str(repo_root / "agent_multirepo.py")]
    if mode == "single":
        return [str(python), str(repo_root / "agentd.py")]
    raise ValueError(f"unsupported launchd mode: {mode!r}")


def build_launch_agent(
    mode: Mode,
    *,
    repo_root: Path,
    home: Path,
    max_workers: int = 2,
    registry_path: Path | None = None,
) -> dict[str, object]:
    repo_root = repo_root.expanduser().resolve()
    home = home.expanduser().resolve()
    logs = home / "Library" / "Logs"
    return {
        "Label": LABEL,
        "ProgramArguments": build_program_arguments(
            mode,
            repo_root=repo_root,
            home=home,
            max_workers=max_workers,
            registry_path=registry_path,
        ),
        "WorkingDirectory": str(repo_root),
        "RunAtLoad": True,
        "KeepAlive": True,
        "ThrottleInterval": 10,
        "ExitTimeOut": LAUNCHD_EXIT_TIMEOUT_SECONDS,
        "ProcessType": "Interactive",
        "EnvironmentVariables": {
            "HOME": str(home),
            "PATH": default_path(home),
            "PYTHONDONTWRITEBYTECODE": "1",
        },
        "StandardOutPath": str(logs / "local-agent.log"),
        "StandardErrorPath": str(logs / "local-agent-error.log"),
    }


def render_launch_agent(
    mode: Mode,
    *,
    repo_root: Path,
    home: Path,
    max_workers: int = 2,
    registry_path: Path | None = None,
) -> bytes:
    payload = build_launch_agent(
        mode,
        repo_root=repo_root,
        home=home,
        max_workers=max_workers,
        registry_path=registry_path,
    )
    return plistlib.dumps(payload, fmt=plistlib.FMT_XML, sort_keys=False)


def validate_checkout(repo_root: Path) -> None:
    required = (
        repo_root / ".venv" / "bin" / "python",
        repo_root / "agent_entrypoint.py",
        repo_root / "agent_multirepo.py",
        repo_root / "agentd.py",
        repo_root / "agent_parallel.py",
        repo_root / "local_agent" / "__init__.py",
        repo_root / "platforms" / "macos" / "local-agent" / "paths.py",
        repo_root / "platforms" / "macos" / "local-agent" / "daemon" / "service.py",
        repo_root / "platforms" / "macos" / "local-agent" / "entrypoint.py",
        repo_root / "platforms" / "macos" / "local-agent" / "supervisor" / "orchestrator.py",
        repo_root / "platforms" / "macos" / "local-agent" / "supervisor" / "serial.py",
        repo_root / "platforms" / "macos" / "local-agent" / "supervisor" / "worker.py",
        repo_root / "platforms" / "macos" / "local-agent" / "repository" / "worker.py",
    )
    missing = [str(path) for path in required if not path.exists()]
    if missing:
        raise FileNotFoundError("missing Local Agent runtime files: " + ", ".join(missing))


def write_launch_agent(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary_path = Path(temporary)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
    finally:
        temporary_path.unlink(missing_ok=True)


def launchctl_target(uid: int | None = None) -> str:
    resolved_uid = os.getuid() if uid is None else uid
    return f"gui/{resolved_uid}/{LABEL}"


def launchctl_domain(uid: int | None = None) -> str:
    resolved_uid = os.getuid() if uid is None else uid
    return f"gui/{resolved_uid}"


def bootout(*, uid: int | None = None, check: bool = False) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["launchctl", "bootout", launchctl_target(uid)],
        text=True,
        capture_output=True,
        check=check,
    )


def bootstrap(plist_path: Path, *, uid: int | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["launchctl", "bootstrap", launchctl_domain(uid), str(plist_path)],
        text=True,
        capture_output=True,
        check=True,
    )


def print_status(*, uid: int | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["launchctl", "print", launchctl_target(uid)],
        text=True,
        capture_output=True,
        check=False,
    )


def wait_until_unloaded(
    *,
    uid: int | None = None,
    timeout: float = UNLOAD_TIMEOUT_SECONDS,
    poll_interval: float = UNLOAD_POLL_SECONDS,
) -> None:
    """Wait until launchd no longer reports the service after bootout."""
    deadline = time.monotonic() + max(0.0, timeout)
    while True:
        if print_status(uid=uid).returncode != 0:
            return
        if time.monotonic() >= deadline:
            raise TimeoutError(f"launchd service {LABEL} did not unload within {timeout:.1f}s")
        time.sleep(max(0.01, poll_interval))


def _is_transient_bootstrap_error(exc: subprocess.CalledProcessError) -> bool:
    if int(exc.returncode) != 5:
        return False
    message = f"{exc.stdout or ''}\n{exc.stderr or ''}".lower()
    return "bad request" in message or "input/output error" in message


def restart_launch_agent(
    plist_path: Path,
    *,
    uid: int | None = None,
    unload_timeout: float = UNLOAD_TIMEOUT_SECONDS,
    poll_interval: float = UNLOAD_POLL_SECONDS,
    bootstrap_attempts: int = BOOTSTRAP_RETRY_ATTEMPTS,
    retry_delay: float = BOOTSTRAP_RETRY_DELAY_SECONDS,
) -> subprocess.CompletedProcess[str]:
    """Restart the LaunchAgent with bounded protection against launchd unload races."""
    if bootstrap_attempts < 1:
        raise ValueError("bootstrap_attempts must be positive")

    bootout(uid=uid, check=False)
    wait_until_unloaded(uid=uid, timeout=unload_timeout, poll_interval=poll_interval)

    for attempt in range(1, bootstrap_attempts + 1):
        try:
            return bootstrap(plist_path, uid=uid)
        except subprocess.CalledProcessError as exc:
            if not _is_transient_bootstrap_error(exc) or attempt == bootstrap_attempts:
                raise
            # A failed bootstrap should not be retried if launchd actually loaded
            # the service despite returning an error.
            if print_status(uid=uid).returncode == 0:
                raise
            time.sleep(max(0.01, retry_delay))

    raise AssertionError("unreachable bootstrap retry loop")