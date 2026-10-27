"""CLI rendering for environment diagnostics."""

from __future__ import annotations

import json
import sys
from collections.abc import Sequence

from local_agent.host_ops.capabilities.remote.ssh import SYSTEM_SCP_EXECUTABLE, SYSTEM_SSH_EXECUTABLE
from local_agent.host_ops.core.diagnostics import CommandRequirement, run_doctor

_DEFAULT_REQUIREMENTS = (
    CommandRequirement(
        SYSTEM_SSH_EXECUTABLE,
        "SSH execution capability will be unavailable",
        required=False,
    ),
    CommandRequirement(
        SYSTEM_SCP_EXECUTABLE,
        "SSH file-transfer capability will be unavailable",
        required=False,
    ),
    CommandRequirement("adb", "Android/ADB capability will be unavailable", required=False),
)


def run(
    *,
    as_json: bool,
    requirements: Sequence[CommandRequirement] = _DEFAULT_REQUIREMENTS,
) -> int:
    report = run_doctor(command_requirements=requirements)

    if as_json:
        print(json.dumps(report.as_dict(), sort_keys=True))
    else:
        for check in report.checks:
            label = check.status.value.upper()
            print(f"[{label}] {check.name}: {check.summary}")
        output = sys.stdout if report.ok else sys.stderr
        print("Doctor: PASS" if report.ok else "Doctor: FAIL", file=output)

    return 0 if report.ok else 1
