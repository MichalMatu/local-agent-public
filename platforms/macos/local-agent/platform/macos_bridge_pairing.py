"""Opt-in, isolated launchd lifecycle for the machine-local Chat Bridge pairing UI.

No GitHub token, dispatch polling, main Local Agent daemon control or Chrome
Send. The installer calls setup only with an explicit pairing opt-in.
"""

from __future__ import annotations

import os
import plistlib
import subprocess
from pathlib import Path

from local_agent.platform.macos_launchd import default_path, write_launch_agent

PAIRING_LABEL = "com.michal.local-agent.chat-bridge-pairing"
PAIRING_PORT = 8768


def pairing_plist_path(home: Path) -> Path:
    return home / "Library" / "LaunchAgents" / f"{PAIRING_LABEL}.plist"


def pairing_service_target(uid: int | None = None) -> str:
    return f"gui/{os.getuid() if uid is None else uid}/{PAIRING_LABEL}"


def render_pairing_launch_agent(*, repo_root: Path, home: Path,
                                transport_enabled: bool = False) -> bytes:
    """User-login sidecar; no key or credential appears in its arguments."""
    repo_root = repo_root.expanduser().resolve()
    home = home.expanduser().resolve()
    python = repo_root / ".venv" / "bin" / "python"
    spool = home / ".local-agent" / "fabric" / "chat-bridge.json"
    payload: dict[str, object] = {
        "Label": PAIRING_LABEL,
        "ProgramArguments": [
            str(python), "-m", "local_agent.conversation.github_first_bridge",
            "--serve" if transport_enabled else "--pair-only",
            "--spool", str(spool), "--port", str(PAIRING_PORT),
        ],
        "WorkingDirectory": str(repo_root),
        "RunAtLoad": True,
        "KeepAlive": True,
        "ThrottleInterval": 10,
        "ProcessType": "Interactive",
        "LimitLoadToSessionType": "Aqua",
        "Umask": 0o077,
        "EnvironmentVariables": {
            "HOME": str(home), "PATH": default_path(home),
            "PYTHONDONTWRITEBYTECODE": "1",
        },
        "StandardOutPath": str(home / "Library" / "Logs" / "local-agent-chat-bridge-pairing.log"),
        "StandardErrorPath": str(home / "Library" / "Logs" / "local-agent-chat-bridge-pairing-error.log"),
    }
    return plistlib.dumps(payload, fmt=plistlib.FMT_XML, sort_keys=False)


def validate_pairing_checkout(repo_root: Path) -> None:
    required = (
        ".venv/bin/python",
        "local_agent/conversation/github_first_bridge.py",
        "local_agent/host_ops/capabilities/local/browser/chat_bridge_pairing.py",
        "local_agent/host_ops/capabilities/local/browser/chat_bridge_http.py",
    )
    missing = [path for path in required if not (repo_root / path).is_file()]
    if missing:
        raise FileNotFoundError("missing Chat Bridge pairing runtime: " + ", ".join(missing))


def setup_pairing_service(*, repo_root: Path, home: Path,
                          runner=subprocess.run) -> str:
    """Install/activate only the pairing sidecar, without restarting anything.

    Repeated setup on an identical active definition is safe. Any active or
    existing *different* definition is a conflict and requires deliberate
    operator reconciliation, never a silent replacement.
    """
    validate_pairing_checkout(repo_root)
    content = render_pairing_launch_agent(repo_root=repo_root, home=home)
    path = pairing_plist_path(home)
    if path.exists() and path.read_bytes() != content:
        raise RuntimeError("Chat Bridge pairing plist differs; explicit operator migration required")
    current = runner(
        ["launchctl", "print", pairing_service_target()],
        text=True, capture_output=True, check=False,
    )
    if current.returncode == 0:
        if path.exists() and path.read_bytes() == content:
            return "already_running"
        raise RuntimeError("Chat Bridge pairing service exists without matching plist")
    # Only a verified missing-service response is safe to treat as unloaded.
    reason = ((current.stderr or "") + " " + (current.stdout or "")).lower()
    if "could not find service" not in reason and "service not found" not in reason:
        raise RuntimeError("Chat Bridge pairing service status unknown; activation refused")
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        write_launch_agent(path, content)
    try:
        runner(
            ["launchctl", "bootstrap", f"gui/{os.getuid()}", str(path)],
            text=True, capture_output=True, check=True,
        )
    except subprocess.CalledProcessError as exc:
        raise RuntimeError("Chat Bridge pairing launchd activation failed") from exc
    return "activated"



def upgrade_pairing_to_transport(*, repo_root: Path, home: Path,
                                 runner=subprocess.run) -> str:
    """Explicitly replace only the pairing sidecar, never the main daemon.

    An active existing pairing service must match the known pairing-only
    argv and label. The old plist is preserved for a bounded rollback if
    launchd activation of the full transport fails.
    """
    validate_pairing_checkout(repo_root)
    path = pairing_plist_path(home)
    if not path.is_file() or path.is_symlink():
        raise RuntimeError("Existing pairing service definition required")
    previous = path.read_bytes()
    old = plistlib.loads(previous)
    new_content = render_pairing_launch_agent(repo_root=repo_root, home=home,
                                               transport_enabled=True)
    current = runner(["launchctl", "print", pairing_service_target()],
                     text=True, capture_output=True, check=False)
    if current.returncode != 0:
        raise RuntimeError("Pairing sidecar must be loaded before transport upgrade")
    if previous == new_content:
        return "already_enabled"
    expected = {
        "Label": PAIRING_LABEL,
        "ProgramArguments": old.get("ProgramArguments"),
    }
    args = expected["ProgramArguments"]
    if (expected["Label"] != old.get("Label") or not isinstance(args, list)
            or len(args) != 8 or args[1:4] != ["-m", "local_agent.conversation.github_first_bridge", "--pair-only"]
            or args[4] != "--spool" or args[6] != "--port"
            or args[7] != str(PAIRING_PORT)
            or not str(args[5]).startswith(str(home.expanduser().resolve() / ".local-agent" / "fabric"))):
        raise RuntimeError("Existing sidecar is not the known pairing-only service")
    # Transport mode uses the *same* bound localhost key and port. No second
    # listener is created, and the original daemon's label is never touched.
    runner(["launchctl", "bootout", pairing_service_target()],
           text=True, capture_output=True, check=True)
    try:
        write_launch_agent(path, new_content)
        runner(["launchctl", "bootstrap", f"gui/{os.getuid()}", str(path)],
               text=True, capture_output=True, check=True)
    except (OSError, subprocess.CalledProcessError):
        write_launch_agent(path, previous)
        try:
            runner(["launchctl", "bootstrap", f"gui/{os.getuid()}", str(path)],
                   text=True, capture_output=True, check=True)
        except (OSError, subprocess.CalledProcessError) as exc:
            raise RuntimeError("Transport upgrade failed; pairing rollback also failed") from exc
        raise RuntimeError("Transport upgrade failed; old pairing service restored") from None
    return "transport_enabled"
