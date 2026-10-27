"""Debian-native /proc process-group RSS sampling; Mac sysctl/top are not used."""
from __future__ import annotations
import os
from pathlib import Path
from typing import Any

def sample_process_group_rss_mb(process_group: int) -> float | None:
    total_kib = 0
    seen = False
    try:
        for directory in Path("/proc").iterdir():
            if not directory.name.isdecimal():
                continue
            try:
                # /proc/[pid]/stat: comm is parenthesized, so split after final ')'.
                fields = (directory / "stat").read_text().rsplit(")", 1)[1].split()
                if int(fields[2]) != process_group:
                    continue
                status = (directory / "status").read_text()
                for line in status.splitlines():
                    if line.startswith("VmRSS:"):
                        total_kib += int(line.split()[1])
                        break
                seen = True
            except (OSError, IndexError, ValueError):
                continue
    except OSError:
        return None
    return total_kib / 1024 if seen else None

def collect_telemetry(pid: int) -> dict[str, Any]:
    try:
        pgid = os.getpgid(pid)
        rss = sample_process_group_rss_mb(pgid)
        return {"command_rss_mb": round(rss, 2)} if rss is not None else {}
    except (OSError, ProcessLookupError):
        return {}
