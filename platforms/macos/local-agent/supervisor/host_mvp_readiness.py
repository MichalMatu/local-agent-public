"""Read-only MVP readiness gate for the selected macOS owner trust boundary.

This is diagnostics, not local administrator enrollment or remote admission.
The sudo/helper check is deliberately a one-way hazard detector: even a clean
report does not certify the absence of other elevation routes or GitHub writers.
"""
from __future__ import annotations

import os
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any

from local_agent.supervisor.host_pairing_boundary import inspect_root_owned_pairing

_HELPER = Path("/usr/local/sbin/local-agent-powermetrics")


def inspect_macos_helper_risk() -> dict[str, Any]:
    """Identify an unsafe trusted sudo path without executing sudo commands."""
    if sys.platform != "darwin":
        return {"status": "unsupported_platform"}
    try:
        policy = subprocess.run(
            ["/usr/bin/sudo", "-n", "-l"],
            capture_output=True, text=True, timeout=10, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return {"status": "sudo_policy_unavailable"}
    if policy.returncode or len(policy.stdout) > 65536:
        return {"status": "sudo_policy_unavailable"}
    rules = [line for line in policy.stdout.splitlines() if "NOPASSWD:" in line]
    if not any(str(_HELPER) in line for line in rules):
        return {"status": "sudo_helper_rule_unverified"}
    try:
        actual = _HELPER.lstat()
        if not stat.S_ISREG(actual.st_mode) or actual.st_uid != 0:
            return {"status": "unsafe_executable"}
        ancestors = list(reversed(_HELPER.parents))
        for parent in ancestors:
            info = parent.lstat()
            if (not stat.S_ISDIR(info.st_mode) or info.st_uid != 0
                    or info.st_mode & 0o022 or os.access(parent, os.W_OK)):
                return {"status": "unsafe_writable_ancestor", "path": str(parent)}
    except OSError:
        return {"status": "helper_path_unavailable"}
    # This does NOT establish ACL, sudoers argument or full elevation safety.
    return {"status": "posix_path_protected_only", "sudo_scope_verified": False}


def assess_owner_mvp_readiness(
    *, record_path: Path, identity_file: Path, expected_repository: str,
) -> dict[str, Any]:
    """Aggregate *observations*, always refuse to grant standing authority."""
    helper = inspect_macos_helper_risk()
    boundary: dict[str, Any]
    try:
        observed = inspect_root_owned_pairing(
            record_path=record_path, identity_file=identity_file,
            expected_repository=expected_repository,
        )
        boundary = {
            "status": "root_state_observed",
            "paired_choice_observed": bool(observed["paired"]),
            "epoch": observed["epoch"],
        }
    except (ValueError, OSError, RuntimeError, UnicodeError):
        boundary = {"status": "root_state_unverified"}
    blockers = []
    if helper["status"] == "unsafe_writable_ancestor":
        blockers.append("sudo_helper_has_service_writable_ancestor")
    elif helper["status"] != "posix_path_protected_only":
        blockers.append("macos_privileged_helper_scope_unverified")
    else:
        blockers.append("full_privilege_and_acl_audit_not_completed")
    if boundary["status"] != "root_state_observed" or not boundary["paired_choice_observed"]:
        blockers.append("protected_local_owner_pairing_not_verified")
    blockers.extend([
        "effective_github_task_writer_policy_not_verified",
        "standing_worker_admission_not_accepted_or_enabled",
    ])
    return {
        "status": "blocked_for_standing_owner_mvp",
        "helper_risk": helper,
        "protected_owner_state": boundary,
        "blockers": blockers,
        "existing_project_task_lane": "separate_and_not_tested_by_this_command",
        "github_writers_verified": False,
        "standing_execution_enabled": False,
        "execution_authority": "not_asserted",
    }
