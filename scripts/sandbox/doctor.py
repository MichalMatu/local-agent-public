"""Read-only Linux sandbox capability and resource preflight.

This is not macOS acceptance. It never downloads or installs packages,
spawns a browser, touches GitHub, or alters Local Agent runtime policy.
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from scripts import verify_local  # noqa: E402

MIB = 1024 * 1024


def available_memory_mib() -> int | None:
    """Conservatively bound Linux MemAvailable by cgroup v2 headroom."""
    try:
        entries = (Path("/proc/meminfo").read_text(encoding="ascii").splitlines())
        mem_kib = next(
            int(line.split()[1]) for line in entries if line.startswith("MemAvailable:")
        )
        available = mem_kib * 1024
    except (OSError, StopIteration, ValueError, IndexError):
        return None
    try:
        limit = Path("/sys/fs/cgroup/memory.max").read_text(encoding="ascii").strip()
        used = int(Path("/sys/fs/cgroup/memory.current").read_text(encoding="ascii").strip())
        if limit != "max":
            available = min(available, max(0, int(limit) - used))
    except (OSError, ValueError):
        pass  # No cgroup limit; /proc/meminfo is the best available bound.
    return available // MIB


def inspect(*, min_memory_mib: int = 1800, min_disk_mib: int = 1024) -> dict[str, object]:
    """Diagnose portable verification readiness without any execution side effects."""
    missing = verify_local.missing_dependencies("sandbox")
    memory = available_memory_mib()
    disk = shutil.disk_usage(ROOT).free // MIB
    if memory is None:
        missing.append("measurable available memory")
    elif memory < min_memory_mib:
        missing.append("available memory below sandbox budget")
    if disk < min_disk_mib:
        missing.append("free disk below sandbox budget")
    return {
        "schema_version": 1,
        "profile": "sandbox",
        "status": "READY" if not missing else "UNVERIFIED",
        "platform": platform.system(),
        "python": platform.python_version(),
        "node": shutil.which("node") is not None,
        "memory_available_mib": memory,
        "disk_free_mib": disk,
        "cpu_count": os.cpu_count(),
        "missing": sorted(set(missing)),
        "not_run": [
            "native macOS smoke and launchd",
            "installed Chrome and authenticated browser Send",
            "real physical devices",
        ],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--min-memory-mib", type=int, default=1800)
    parser.add_argument("--min-disk-mib", type=int, default=1024)
    args = parser.parse_args(argv)
    if args.min_memory_mib < 1 or args.min_disk_mib < 1:
        parser.error("resource thresholds must be positive")
    report = inspect(min_memory_mib=args.min_memory_mib, min_disk_mib=args.min_disk_mib)
    print(json.dumps(report, sort_keys=True, separators=(",", ":")), flush=True)
    return 0 if report["status"] == "READY" else 2


if __name__ == "__main__":
    raise SystemExit(main())
