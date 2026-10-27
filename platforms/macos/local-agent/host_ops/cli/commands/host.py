"""CLI rendering for generic local host profile facts."""

from __future__ import annotations

import json
import sys

from local_agent.host_ops.capabilities.local.host import (
    HostCapacityProfiler, HostOverviewCollector, HostProfileError, HostProfiler,
)
from local_agent.host_ops.core.execution import ExecutionLimits


def run_profile(*, timeout_seconds: float, as_json: bool) -> int:
    try:
        profile = HostProfiler().inspect(
            limits=ExecutionLimits(
                timeout_seconds=timeout_seconds,
                max_stdout_bytes=8192,
                max_stderr_bytes=8192,
            )
        )
    except (HostProfileError, ValueError) as exc:
        if as_json:
            print(json.dumps({"error": str(exc)}, sort_keys=True), file=sys.stderr)
        else:
            print(f"host profile failed: {exc}", file=sys.stderr)
        return 1

    payload = profile.as_dict()
    if as_json:
        print(json.dumps(payload, sort_keys=True))
    else:
        print(" ".join(f"{key}={value}" for key, value in payload.items() if value is not None))
    return 0


def run_capacity(*, timeout_seconds: float, as_json: bool) -> int:
    """Render an instantaneous, read-only resource snapshot."""
    try:
        snapshot = HostCapacityProfiler().inspect(
            limits=ExecutionLimits(
                timeout_seconds=timeout_seconds,
                max_stdout_bytes=8192,
                max_stderr_bytes=8192,
            )
        )
    except (HostProfileError, ValueError) as exc:
        if as_json:
            print(json.dumps({"error": str(exc)}, sort_keys=True), file=sys.stderr)
        else:
            print(f"host capacity failed: {exc}", file=sys.stderr)
        return 1
    payload = snapshot.as_dict()
    if as_json:
        print(json.dumps(payload, sort_keys=True))
    else:
        print(" ".join(f"{key}={value}" for key, value in payload.items() if value is not None))
    return 0


def run_overview(*, timeout_seconds: float, names: tuple[str, ...], as_json: bool) -> int:
    """Aggregate a bounded local snapshot, without exposing full tool paths."""
    try:
        overview = HostOverviewCollector().inspect(
            names=names,
            limits=ExecutionLimits(
                timeout_seconds=timeout_seconds,
                max_stdout_bytes=8192,
                max_stderr_bytes=8192,
            ),
        )
    except (HostProfileError, ValueError) as exc:
        if as_json:
            print(json.dumps({"error": str(exc)}, sort_keys=True), file=sys.stderr)
        else:
            print(f"host overview failed: {exc}", file=sys.stderr)
        return 1
    if as_json:
        print(json.dumps(overview, sort_keys=True))
    else:
        _print_overview(overview)
    return 0


def _format_bytes(value: int | None) -> str:
    if value is None:
        return "unknown"
    amount = float(value)
    for suffix in ("B", "KiB", "MiB", "GiB", "TiB"):
        if amount < 1024 or suffix == "TiB":
            return f"{amount:.1f} {suffix}" if suffix != "B" else f"{int(amount)} B"
        amount /= 1024
    raise AssertionError("unreachable")


def _load_text(value: float | None) -> str:
    return f"{value:.2f}" if value is not None else "unknown"


def _print_overview(overview: dict[str, object]) -> None:
    profile = overview["profile"]
    capacity = overview["capacity"]
    tools = overview["tools"]
    print(f"Host: {profile['hostname']} | {profile['system']} {profile['release']} "
          f"| {profile['architecture']}")
    print(f"CPU: {profile['logical_cpu_count'] or 'unknown'} logical | "
          f"load 1/5/15m: "
          f"{_load_text(capacity['load_average_1m'])} / "
          f"{_load_text(capacity['load_average_5m'])} / "
          f"{_load_text(capacity['load_average_15m'])} (not CPU percent)")
    kind = capacity["memory_availability_kind"]
    source = {
        "linux_memavailable": "Linux kernel estimate",
        "macos_vm_stat_estimate": "macOS reclaimable-page estimate",
    }.get(kind, "unavailable")
    print(f"Memory: {_format_bytes(capacity['memory_available_bytes'])} available "
          f"({source}) / {_format_bytes(profile['memory_total_bytes'])} total")
    print(f"Root disk: {_format_bytes(capacity['root_free_bytes'])} free / "
          f"{_format_bytes(capacity['root_total_bytes'])} total")
    print("Tools:")
    for tool in tools:
        status = (tool["version"] or "version unknown") if tool["present"] else "not found"
        suffix = " (version probe failed)" if tool["error"] else ""
        print(f"  {tool['name']}: {status}{suffix}")
    print(f"Observed: {overview['observed_at_utc']} (capacity sample)")
    print("Diagnostic only: not reserved capacity or idle worker slots.")
