"""Read-only host service metadata for source-rollout preflight.

This never installs, stops, restarts or changes a service. Service metadata
alone cannot authorize a source switch or guarantee queue quiescence.
"""
from __future__ import annotations

import os
from pathlib import Path
import re
import subprocess
from typing import Callable

from scripts.isolated_rollout_controller import RolloutRejected

_UNIT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.@-]{0,127}\.service$")
_PROPERTY_KEYS = frozenset({"ActiveState", "SubState", "MainPID", "WorkingDirectory",
                            "NeedDaemonReload"})
_MAX_OUTPUT = 4096


def read_systemd_service(unit: str, expected_source: Path, *,
                         runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
                         verify_pid: bool = True) -> dict:
    """Inspect one explicitly named service without invoking a privileged operation."""
    if not isinstance(unit, str) or _UNIT.fullmatch(unit) is None:
        raise RolloutRejected("invalid explicit systemd service name")
    if expected_source.is_symlink() or not expected_source.is_dir():
        raise RolloutRejected("expected source is not a real directory")
    try:
        result = runner(
            ["systemctl", "show", unit, "--no-pager",
             "-p", "ActiveState", "-p", "SubState", "-p", "MainPID",
             "-p", "WorkingDirectory", "-p", "NeedDaemonReload"],
            capture_output=True, text=True, timeout=15, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RolloutRejected("systemd read-only inspection unavailable") from exc
    if result.returncode != 0 or not isinstance(result.stdout, str) or len(result.stdout) > _MAX_OUTPUT:
        raise RolloutRejected("systemd service inspection failed or exceeded bound")
    properties: dict[str, str] = {}
    for line in result.stdout.splitlines():
        if "=" not in line:
            raise RolloutRejected("invalid systemd show output")
        key, value = line.split("=", 1)
        if key not in _PROPERTY_KEYS or key in properties:
            raise RolloutRejected("unexpected or duplicated systemd property")
        properties[key] = value
    if set(properties) != _PROPERTY_KEYS:
        raise RolloutRejected("incomplete systemd service metadata")
    if properties["ActiveState"] != "active" or properties["SubState"] != "running":
        raise RolloutRejected("systemd service is not active/running")
    if properties["NeedDaemonReload"] != "no":
        raise RolloutRejected("systemd unit has unapplied configuration")
    try:
        pid = int(properties["MainPID"])
    except ValueError as exc:
        raise RolloutRejected("invalid systemd MainPID") from exc
    if not 1 <= pid <= 2**31 - 1:
        raise RolloutRejected("no live systemd MainPID")
    expected = expected_source.resolve(strict=True)
    source = Path(properties["WorkingDirectory"])
    if not source.is_absolute() or source.is_symlink() or not source.is_dir():
        raise RolloutRejected("systemd working directory is invalid")
    if source.resolve(strict=True) != expected:
        raise RolloutRejected("systemd service points at a different source checkout")
    if verify_pid:
        # Existence alone does not guarantee a stable PID; this is an
        # observational check, and the deployment fence must revalidate.
        try:
            os.kill(pid, 0)
        except (ProcessLookupError, PermissionError) as exc:
            raise RolloutRejected("systemd MainPID unavailable to observer") from exc
    return {
        "service_manager": "systemd",
        "unit": unit,
        "state": "active",
        "main_pid": pid,
        "working_directory": str(expected),
        "source_switch_authorized": False,
        "exclusive_queue_admission_fence": False,
    }


def offline_systemd_watchdog_template(*, sandbox: Path, python_executable: Path,
                                       checkout: Path) -> str:
    """Render an uninstalled sandbox-only user service for manual review.

    The caller must separately stage a supervised unit and prove reboot
    behavior. This helper neither writes the unit nor invokes systemctl.
    """
    from scripts.isolated_rollout_controller import _sandbox
    root = _sandbox(sandbox)
    paths = (python_executable, checkout)
    if any(not p.is_absolute() or "\n" in str(p) or "%" in str(p)
           or " " in str(p) for p in paths + (root,)):
        raise RolloutRejected("unsafe systemd watchdog unit path")
    if not python_executable.is_file() or not checkout.is_dir():
        raise RolloutRejected("missing watchdog interpreter or source")
    return (
        "[Unit]\n"
        "Description=Local Agent isolated source rollback rehearsal\n"
        "ConditionPathExists=" + str(root / ".isolated-source-rollout-sandbox") + "\n"
        "[Service]\n"
        "Type=oneshot\n"
        "WorkingDirectory=" + str(checkout) + "\n"
        "ExecStart=" + str(python_executable) + " -m scripts.isolated_rollout_watchdog " + str(root) + "\n"
        "NoNewPrivileges=yes\n"
        "PrivateTmp=no\n"
    )


def offline_macos_watchdog_plist(*, sandbox: Path, python_executable: Path,
                                checkout: Path) -> bytes:
    """Produce a disabled, uninstalled launchd rehearsal plist.

    RunAtLoad is false and Disabled is true: a real supervisor and reboot
    acceptance would require a separate operator-approved installation.
    """
    import plistlib

    from scripts.isolated_rollout_controller import _sandbox
    root = _sandbox(sandbox)
    if any(not path.is_absolute() or "\x00" in str(path) or "\n" in str(path)
           for path in (python_executable, checkout, root)):
        raise RolloutRejected("invalid offline launchd path")
    if not python_executable.is_file() or not checkout.is_dir():
        raise RolloutRejected("offline launchd source or interpreter missing")
    return plistlib.dumps({
        "Label": "dev.localagent.rollout.sandbox",
        "Disabled": True,
        "RunAtLoad": False,
        "StartInterval": 30,
        "WorkingDirectory": str(checkout),
        "ProgramArguments": [
            str(python_executable), "-m",
            "scripts.isolated_rollout_watchdog", str(root),
        ],
    })
