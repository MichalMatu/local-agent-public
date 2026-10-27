"""Mac-only, uninstalled boot-recovery LaunchAgent design.

A disabled plist with RunAtLoad=True documents *intended* login behavior
only. No bootstrap, activation, installation, live state or privilege path.
The recovery source must be an independently pinned, private checkout,
not the candidate pointer being rolled back.
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import plistlib
import stat
import sys
import tempfile
from typing import Mapping

from scripts import isolated_rollout_controller as rollout

_PINNED_MODULES = (
    "scripts/isolated_rollout_controller.py",
    "scripts/isolated_rollout_fence.py",
    "scripts/macos_rollout_boot_recovery.py",
)
_LABEL = "dev.localagent.macos-b1c.sandbox-recovery"
_MAX_MODULE_BYTES = 256 * 1024


def _pinned_recovery_source(checkout: Path, expected: Mapping[str, str]) -> dict[str, str]:
    if not isinstance(expected, dict) or set(expected) != set(_PINNED_MODULES):
        raise rollout.RolloutRejected("incomplete independent Mac recovery source pins")
    digests = {}
    for relative in _PINNED_MODULES:
        required = expected[relative]
        if (not isinstance(required, str) or len(required) != 64
                or any(c not in "0123456789abcdef" for c in required)):
            raise rollout.RolloutRejected("invalid Mac recovery SHA-256 pin")
        path = checkout / relative
        if path.is_symlink():
            raise rollout.RolloutRejected("Mac recovery source symlink rejected")
        try:
            fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        except OSError as exc:
            raise rollout.RolloutRejected("pinned Mac recovery module missing") from exc
        try:
            info = os.fstat(fd)
            if (not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= _MAX_MODULE_BYTES
                    or info.st_uid != os.getuid() or info.st_mode & 0o022):
                raise rollout.RolloutRejected("Mac recovery module ownership/mode invalid")
            with os.fdopen(fd, "rb", closefd=False) as handle:
                raw = handle.read(_MAX_MODULE_BYTES + 1)
        finally:
            os.close(fd)
        if len(raw) > _MAX_MODULE_BYTES:
            raise rollout.RolloutRejected("Mac recovery module oversized")
        digest = hashlib.sha256(raw).hexdigest()
        if digest != required:
            raise rollout.RolloutRejected("trusted Mac recovery source pin mismatch")
        digests[relative] = digest
    return digests


def render_disabled_boot_recovery_plan(
    *, sandbox: Path, python_executable: Path,
    stable_checkout: Path, trusted_module_sha256: Mapping[str, str],
) -> bytes:
    """Return an uninstalled, disabled plist with a pinned stable code source.

    Even exact matching pins are source verification, not deployment authority.
    True RunAtLoad requires a future deliberate operator-enabled installation.
    """
    if sys.platform != "darwin":
        raise rollout.RolloutRejected("Mac recovery plan is macOS-only")
    root = rollout._sandbox(sandbox)
    original = Path(stable_checkout)
    try:
        checkout = original.resolve(strict=True)
    except OSError as exc:
        raise rollout.RolloutRejected("stable Mac recovery checkout missing") from exc
    temp_root = Path(tempfile.gettempdir()).resolve(strict=True)
    if (original.is_symlink() or not original.is_absolute()
            or checkout == root or root in checkout.parents
            or temp_root not in checkout.parents
            or not checkout.is_dir()
            or not stat.S_ISDIR(checkout.lstat().st_mode)):
        raise rollout.RolloutRejected("stable recovery checkout is not independent")
    if checkout != original:
        # Do not hide redirected workspaces or OS temp-root aliases.
        # Require the canonical path for this *offline* plan.
        raise rollout.RolloutRejected("recovery checkout contains redirected ancestor")
    info = checkout.stat()
    if info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise rollout.RolloutRejected("stable recovery checkout must be private")
    scripts_dir = checkout / "scripts"
    if scripts_dir.is_symlink() or not scripts_dir.is_dir():
        raise rollout.RolloutRejected("stable recovery scripts directory unsafe")
    script_info = scripts_dir.stat()
    if script_info.st_uid != os.getuid() or script_info.st_mode & 0o022:
        raise rollout.RolloutRejected("stable recovery scripts directory not trusted")
    # The repository uses a namespace package. Installing an unpinned
    # __init__.py would introduce code executed ahead of pinned modules.
    initializer = scripts_dir / "__init__.py"
    if initializer.exists() or initializer.is_symlink():
        raise rollout.RolloutRejected("unexpected Mac recovery package initializer")
    _pinned_recovery_source(checkout, trusted_module_sha256)

    try:
        interpreter = Path(python_executable).resolve(strict=True)
    except OSError as exc:
        raise rollout.RolloutRejected("trusted Mac recovery Python interpreter missing") from exc
    if not interpreter.is_file() or not interpreter.stat().st_mode & 0o111:
        raise rollout.RolloutRejected("trusted Mac recovery Python interpreter missing")
    # launchd ProgramArguments are exact argv tokens; no shell interpolation.
    return plistlib.dumps({
        "Label": _LABEL,
        "Disabled": True,
        "RunAtLoad": True,
        "KeepAlive": False,
        "StartInterval": 30,
        "ProcessType": "Background",
        "WorkingDirectory": str(checkout),
        "EnvironmentVariables": {"PYTHONDONTWRITEBYTECODE": "1"},
        "ProgramArguments": [
            str(interpreter), "-B", "-m",
            "scripts.macos_rollout_boot_recovery", str(root),
        ],
    }, fmt=plistlib.FMT_XML, sort_keys=True)


def inspect_disabled_boot_recovery_plan(
    payload: bytes, *, sandbox: Path, python_executable: Path,
    stable_checkout: Path, trusted_module_sha256: Mapping[str, str],
) -> dict[str, object]:
    """Fail closed on a tampered offline plan; always deny live activation."""
    if type(payload) is not bytes or not 0 < len(payload) <= 16 * 1024:
        raise rollout.RolloutRejected("offline launchd plist exceeds bound")
    expected = render_disabled_boot_recovery_plan(
        sandbox=sandbox, python_executable=python_executable,
        stable_checkout=stable_checkout, trusted_module_sha256=trusted_module_sha256,
    )
    try:
        proposed = plistlib.loads(payload)
        canonical = plistlib.loads(expected)
    except (TypeError, ValueError, OverflowError) as exc:
        raise rollout.RolloutRejected("invalid offline Mac launchd plist") from exc
    if proposed != canonical:
        raise rollout.RolloutRejected("Mac launchd plan policy/source mismatch")
    return {
        "version": 1,
        "offline_only": True,
        "source_pins_verified": True,
        "recovery_source_outside_candidate": True,
        "login_start_policy_rehearsed": True,
        "installed": False,
        "boot_tested": False,
        "source_switch_authorized": False,
        "launchd_service_changed": False,
    }
