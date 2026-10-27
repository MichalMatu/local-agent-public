"""Report native macOS execution conditions without treating historical swap as pressure.

Native-only verification requires a real macOS host. CPU load and allocated
swap are *diagnostics*, not reliable predictors of currently available memory.
Actual work is bounded by the existing command/stage timeouts, watchdogs and
per-task memory limits; do not fail preflight solely for historical swap use.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys

_SWAP_RE = re.compile(r"\btotal\s*=\s*([\d.]+)M\b.*\bused\s*=\s*([\d.]+)M\b")


def check_resources() -> dict[str, object]:
    reasons: list[str] = []
    warnings: list[str] = []
    report: dict[str, object] = {"schema_version": 1, "profile": "macos-native"}
    if sys.platform != "darwin":
        reasons.append("requires real macOS")
    else:
        try:
            cores = os.cpu_count()
            load1 = os.getloadavg()[0]
            if not cores or cores < 1 or load1 < 0:
                raise ValueError("invalid cpu/load measurement")
            report.update(cpu_count=cores, load_1m=round(load1, 2))
            if load1 >= cores:
                warnings.append("high 1-minute load may lengthen verification stages")
        except (OSError, ValueError):
            warnings.append("CPU/load measurement unavailable")
        try:
            result = subprocess.run(
                ["sysctl", "-n", "vm.swapusage"], capture_output=True, text=True,
                check=False, timeout=5,
            )
            if result.returncode:
                raise ValueError("sysctl failed")
            match = _SWAP_RE.search(result.stdout)
            if match is None:
                raise ValueError("unrecognized swap report")
            total, used = map(float, match.groups())
            if total < 0 or used < 0 or used > total + 1:
                raise ValueError("invalid swap values")
            report.update(swap_total_mib=round(total), swap_used_mib=round(used))
            if total >= 4096 and used / total >= 0.75:
                warnings.append(
                    "high allocated swap is historical usage, not proof of active memory pressure"
                )
        except (OSError, subprocess.TimeoutExpired, ValueError):
            warnings.append("swap measurement unavailable")
    report["status"] = "READY" if not reasons else "UNVERIFIED"
    report["reasons"] = reasons
    report["warnings"] = warnings
    return report


def main() -> int:
    outcome = check_resources()
    print(json.dumps(outcome, sort_keys=True, separators=(",", ":")), flush=True)
    return 0 if outcome["status"] == "READY" else 2


if __name__ == "__main__":
    raise SystemExit(main())
