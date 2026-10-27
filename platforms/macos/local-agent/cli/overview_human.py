"""Read-only human presentation of already observed Local Agent facts.

Never infer task admission, free worker slots or a replay decision from
machine capacity, status files or durable records.
"""
from __future__ import annotations

from datetime import datetime, timezone
import math
import re
from typing import Any


def _object(value: object) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _safe_text(value: object, *, fallback: str = "unverified", limit: int = 84) -> str:
    if not isinstance(value, str) or not value:
        return fallback
    first = re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", value.splitlines()[0])
    clean = "".join(character for character in first if 32 <= ord(character) <= 126)
    return clean[:limit] or fallback


def _nonnegative(value: object) -> int | None:
    return value if type(value) is int and 0 <= value <= 2**63 - 1 else None


def _bytes(value: object) -> str:
    amount = _nonnegative(value)
    if amount is None:
        return "unverified"
    if amount < 1024:
        return f"{amount} B"
    size = float(amount)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB", "PiB", "EiB"):
        if size < 1024 or unit == "EiB":
            return f"{size:.1f} {unit}"
        size /= 1024
    return "unverified"


def _load(value: object) -> str:
    if type(value) not in (float, int) or not 0 <= value < 1_000_000 or not math.isfinite(value):
        return "unverified"
    return f"{value:.2f}"


def _sample_age(value: object, now: datetime) -> str:
    if not isinstance(value, str) or len(value) > 48:
        return "unverified"
    try:
        instant = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if instant.tzinfo is None or instant.utcoffset() is None:
            return "unverified"
        seconds = (now - instant.astimezone(timezone.utc)).total_seconds()
    except (ValueError, OverflowError):
        return "unverified"
    if seconds < -30:
        return "unverified (future timestamp)"
    age = max(0, int(seconds))
    if age > 180:
        return f"stale ({age}s old)"
    return f"recent ({age}s old)"


def render_overview_human(
    report: dict[str, object], *, now: datetime | None = None,
) -> str:
    """Render a bounded advisory view; JSON is the unchanged primary API."""
    observed_now = now or datetime.now(timezone.utc)
    if observed_now.tzinfo is None or observed_now.utcoffset() is None:
        raise ValueError("render time must be timezone-aware")
    observed_now = observed_now.astimezone(timezone.utc)
    health = _object(report.get("health"))
    summary = _object(report.get("summary"))
    status = _safe_text(report.get("status"))
    age = _nonnegative(health.get("status_age_seconds"))
    health_status = _safe_text(health.get("status"))
    status_age = f"{age}s old" if age is not None else "unverified"

    lines = [
        f"Local Agent: {status}",
        f"Daemon evidence: {health_status} | heartbeat {status_age}",
    ]
    if summary.get("active_workers_observed") is True:
        count = _nonnegative(summary.get("active_worker_count"))
        limit = _nonnegative(summary.get("max_parallel_workers_observed"))
        if count is not None and limit is not None and 1 <= limit <= 64 and count <= limit:
            lines.append(f"Observed workers: {count}/{limit} busy/limit (NOT free slots)")
        else:
            lines.append("Observed workers: unverified")
    else:
        lines.append("Observed workers: unverified (NOT zero)")

    counts = _object(summary.get("record_counts"))
    keys = ("claims", "result-spool", "corrupt-claims")
    lines.append("Durable records: " + ", ".join(
        f"{key}={_nonnegative(counts.get(key)) if _nonnegative(counts.get(key)) is not None else 'unverified'}"
        for key in keys
    ))
    resources = report.get("host_resources")
    if isinstance(resources, dict) and resources.get("status") == "unverified":
        lines.append("Physical resources: unverified")
    elif isinstance(resources, dict):
        capacity = _object(resources.get("capacity"))
        profile = _object(resources.get("profile"))
        lines.append("Physical capacity sample: " +
                     _sample_age(resources.get("observed_at_utc"), observed_now))
        memory_kind = capacity.get("memory_availability_kind")
        source_kind = {
            "linux_memavailable": "Linux MemAvailable estimate",
            "macos_vm_stat_estimate": "macOS reclaimable-page estimate",
        }.get(memory_kind if isinstance(memory_kind, str) else None, "OS estimate")
        lines.append(
            "RAM available: " + _bytes(capacity.get("memory_available_bytes")) +
            f" ({source_kind}, not reserved)"
        )
        lines.append(
            "OS load 1/5/15m: " + " / ".join(
                _load(capacity.get(key)) for key in (
                    "load_average_1m", "load_average_5m", "load_average_15m"
                )
            ) + " (NOT CPU utilization %)"
        )
        lines.append(
            "Root disk free: " + _bytes(capacity.get("root_free_bytes")) +
            " (not reserved)"
        )
        cpus = _nonnegative(profile.get("logical_cpu_count"))
        lines.append(f"Logical CPUs: {cpus if cpus is not None else 'unverified'}")
        tools = resources.get("tools")
        if isinstance(tools, list):
            for tool in tools[:12]:
                entry = _object(tool)
                name = _safe_text(entry.get("name"), limit=32)
                if entry.get("present") is True and entry.get("error") is None:
                    version = _safe_text(entry.get("version"), fallback="version unverified")
                    lines.append(f"Tool {name}: {version}")
                elif entry.get("present") is False:
                    lines.append(f"Tool {name}: not found")
                else:
                    lines.append(f"Tool {name}: unverified")
    else:
        lines.append("Physical resources: not sampled (--with-host-resources)")

    warnings = report.get("warnings")
    if isinstance(warnings, list) and warnings:
        lines.append("Warnings: " + ", ".join(
            _safe_text(value, limit=64) for value in warnings[:20]
        ))
    lines.append("Advisory only: non-atomic observations; no admission, restart or replay authority.")
    return "\n".join(lines)
