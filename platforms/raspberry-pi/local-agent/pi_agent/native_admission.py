"""Read-only physical Pi native-source admission while historic B2 stays online.

This is NOT an installer or service activator. It proves only basic source
test safety, not rollback, project task/result parity or physical cutover.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Callable

from pi_agent.preflight import read_host

MIN_TEST_AVAILABLE_KIB = 96 * 1024
SYSTEM_UNIT = "local-agent.service"
CANDIDATE_UNIT = "local-agent-pi.service"
_INTEGER = re.compile(r"^[0-9]+$")


class NativeAdmissionBlocked(RuntimeError):
    """Stop source tests rather than guessing Pi service or RAM state."""


def _systemctl(*args: str, runner: Callable = subprocess.run) -> dict[str, str]:
    """Query only systemd's property API; never call a mutating verb."""
    try:
        completed = runner(
            ["systemctl", *args[:-1], "show", args[-1], "--no-pager",
             "-p", "LoadState", "-p", "ActiveState",
             "-p", "MainPID"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, timeout=8, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise NativeAdmissionBlocked("systemd unit status unavailable") from exc
    if completed.returncode != 0:
        raise NativeAdmissionBlocked("systemd unit status unavailable")
    result: dict[str, str] = {}
    for row in completed.stdout.splitlines():
        key, sep, value = row.partition("=")
        if not sep or key in result or key not in {"LoadState", "ActiveState", "MainPID"}:
            raise NativeAdmissionBlocked("malformed systemd status")
        result[key] = value.strip()
    if set(result) != {"LoadState", "ActiveState", "MainPID"}:
        raise NativeAdmissionBlocked("incomplete systemd status")
    if not _INTEGER.fullmatch(result["MainPID"]):
        raise NativeAdmissionBlocked("invalid systemd MainPID")
    return result


def _available_kib(*, meminfo: Path = Path("/proc/meminfo")) -> int:
    try:
        rows = meminfo.read_text(encoding="utf-8").splitlines()
        values = [line.partition(":")[2].strip()
                  for line in rows if line.startswith("MemAvailable:")]
        if len(values) != 1:
            raise ValueError("missing MemAvailable")
        parts = values[0].split()
        if (len(parts) != 2 or parts[1] != "kB"
                or not _INTEGER.fullmatch(parts[0])):
            raise ValueError("invalid MemAvailable")
        return int(parts[0])
    except (ValueError, OSError, UnicodeError) as exc:
        raise NativeAdmissionBlocked("unavailable physical memory evidence") from exc


def check_source_test_admission(
    *, platform: dict[str, str], b2: dict[str, str],
    candidate: dict[str, str], available_kib: int,
) -> dict[str, object]:
    """Require old B2 to remain the ONLY active Pi agent during source QA."""
    if platform.get("platform") != "raspberry-pi-zero-2-w" or platform.get("arch") != "aarch64":
        raise NativeAdmissionBlocked("physical accepted Pi Zero 2 W ARM64 required")
    if (b2.get("LoadState") != "loaded" or b2.get("ActiveState") != "active"
            or not _INTEGER.fullmatch(b2.get("MainPID", ""))
            or int(b2["MainPID"]) < 1):
        raise NativeAdmissionBlocked("historic B2 must be active and identified")
    if (candidate.get("ActiveState") != "inactive"
            or candidate.get("LoadState") not in {"not-found", "loaded"}
            or candidate.get("MainPID") != "0"):
        raise NativeAdmissionBlocked("independent Pi agent may already be running")
    if type(available_kib) is not int or available_kib < MIN_TEST_AVAILABLE_KIB:
        raise NativeAdmissionBlocked("insufficient available RAM for bounded source tests")
    return {
        "status": "source_test_admitted_not_deployed",
        "platform": platform["platform"],
        "arch": platform["arch"],
        "b2_system_unit": SYSTEM_UNIT,
        "b2_main_pid": int(b2["MainPID"]),
        "candidate_user_unit": CANDIDATE_UNIT,
        "candidate_active": False,
        "available_kib": available_kib,
        "minimum_test_available_kib": MIN_TEST_AVAILABLE_KIB,
        "physical_project_e2e_verified": False,
        "rollback_verified": False,
    }


def inspect() -> dict[str, object]:
    """No filesystem mutation and no OS/process control side effects."""
    return check_source_test_admission(
        platform=read_host(),
        b2=_systemctl("--system", SYSTEM_UNIT),
        candidate=_systemctl("--user", CANDIDATE_UNIT),
        available_kib=_available_kib(),
    )


def main() -> int:
    try:
        result = inspect()
    except (NativeAdmissionBlocked, ValueError) as exc:
        print(json.dumps({"status": "source_test_blocked", "reason": str(exc)},
                         sort_keys=True), file=sys.stderr)
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
