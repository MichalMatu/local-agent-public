from __future__ import annotations

import math
import os
import platform
import re
from pathlib import Path
from typing import Any

from local_agent.foundation.process import run_argv_bounded

def _safe_command(args: list[str], timeout: float = 2.0) -> str | None:
    try:
        result = run_argv_bounded(
            args,
            cwd=Path.cwd(),
            env=os.environ,
            timeout=timeout,
            output_limit=1024 * 1024,
            log=lambda _message: None,
        )
    except OSError:
        return None
    return str(result["output"]) if result["exit_code"] == 0 else None


def parse_mac_vm_stat(text: str, *, page_size: int = 4096) -> dict[str, int]:
    page_size_match = re.search(r"page size of\s+(\d+)\s+bytes", text, re.IGNORECASE)
    if page_size_match is not None:
        parsed_page_size = int(page_size_match.group(1))
        if parsed_page_size > 0:
            page_size = parsed_page_size
    pages: dict[str, int] = {}
    for line in text.splitlines():
        match = re.match(r"^Pages ([^:]+):\s+(\d+)", line)
        if match:
            pages[match.group(1).lower()] = int(match.group(2))
    available = sum(pages.get(name, 0) for name in ("free", "inactive", "speculative"))
    used = max(0, sum(pages.values()) - available)
    return {
        "available_bytes": available * page_size,
        "used_bytes": used * page_size,
    }


def parse_mac_swapusage(text: str) -> dict[str, int]:
    values: dict[str, int] = {}
    for name, number, unit in re.findall(
        r"(total|used|free)\s*=\s*([0-9.]+)([MG])B?", text
    ):
        multiplier = 1024**2 if unit == "M" else 1024**3
        values[name] = int(float(number) * multiplier)
    return values


def parse_mac_top_cpu(text: str) -> float | None:
    match = re.search(
        r"CPU usage:\s*([0-9.]+)%\s*user,\s*([0-9.]+)%\s*sys,\s*([0-9.]+)%\s*idle",
        text,
    )
    if match is None:
        return None
    try:
        return round(float(match.group(1)) + float(match.group(2)), 2)
    except ValueError:
        return None


def normalize_host_cpu_percent(total_cpu_percent: float, logical_cpu_count: int) -> float:
    """Normalize summed per-process CPU percentages to host utilization."""
    divisor = max(1, logical_cpu_count)
    return round(max(0.0, min(100.0, total_cpu_percent / divisor)), 2)


def parse_mac_ps_cpu(text: str, logical_cpu_count: int) -> float | None:
    total = 0.0
    parsed = False
    for line in text.splitlines():
        try:
            value = float(line.strip())
        except (TypeError, ValueError):
            continue
        if not math.isfinite(value):
            continue
        total += value
        parsed = True
    if not parsed:
        return None
    return normalize_host_cpu_percent(total, logical_cpu_count)


def parse_process_group_ps(text: str, process_group: int) -> dict[str, Any]:
    cpu = 0.0
    rss_kb = 0
    processes = 0
    for line in text.splitlines():
        fields = line.split()
        if len(fields) < 4:
            continue
        try:
            if int(fields[1]) != process_group:
                continue
            cpu += float(fields[2])
            rss_kb += int(fields[3])
            processes += 1
        except (TypeError, ValueError):
            continue
    if processes == 0:
        return {}
    return {
        "command_cpu_percent": round(cpu, 2),
        "command_rss_mb": round(rss_kb / 1024, 2),
        "command_children": max(0, processes - 1),
    }


def collect_host_telemetry() -> dict[str, Any]:
    telemetry: dict[str, Any] = {}
    try:
        telemetry["host_load_1m"] = round(float(os.getloadavg()[0]), 2)
    except (AttributeError, OSError, IndexError, TypeError, ValueError):
        pass

    if platform.system() == "Darwin":
        total_raw = _safe_command(["sysctl", "-n", "hw.memsize"])
        vm_stat = _safe_command(["vm_stat"])
        if total_raw and vm_stat:
            try:
                total = int(total_raw.strip())
                parsed = parse_mac_vm_stat(vm_stat)
                available = parsed["available_bytes"]
                telemetry["host_memory_available_mb"] = round(available / 1024**2, 2)
                telemetry["host_memory_used_percent"] = round(
                    max(0.0, min(100.0, (total - available) / total * 100)), 2
                )
            except (TypeError, ValueError, ZeroDivisionError, KeyError):
                pass
        swap = _safe_command(["sysctl", "-n", "vm.swapusage"])
        if swap:
            parsed_swap = parse_mac_swapusage(swap)
            if "used" in parsed_swap:
                telemetry["host_swap_used_mb"] = round(parsed_swap["used"] / 1024**2, 2)
            if "total" in parsed_swap:
                telemetry["host_swap_total_mb"] = round(parsed_swap["total"] / 1024**2, 2)
        cpu_text = _safe_command(["top", "-l", "1", "-n", "0"])
        top_cpu = parse_mac_top_cpu(cpu_text or "")
        if top_cpu is not None:
            telemetry["host_cpu_percent"] = top_cpu
        else:
            cpu_text = _safe_command(["ps", "-A", "-o", "%cpu="])
        if cpu_text and "host_cpu_percent" not in telemetry:
            host_cpu = parse_mac_ps_cpu(cpu_text, os.cpu_count() or 1)
            if host_cpu is not None:
                telemetry["host_cpu_percent"] = host_cpu
    else:
        try:
            memory: dict[str, int] = {}
            for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
                key, _, value = line.partition(":")
                fields = value.split()
                if fields and fields[0].isdigit():
                    memory[key] = int(fields[0]) * 1024
            total = memory.get("MemTotal")
            available = memory.get("MemAvailable")
            if total and available is not None:
                telemetry["host_memory_available_mb"] = round(available / 1024**2, 2)
                telemetry["host_memory_used_percent"] = round(
                    (total - available) / total * 100, 2
                )
            swap_total = memory.get("SwapTotal")
            swap_free = memory.get("SwapFree")
            if swap_total is not None and swap_free is not None:
                telemetry["host_swap_total_mb"] = round(swap_total / 1024**2, 2)
                telemetry["host_swap_used_mb"] = round(
                    (swap_total - swap_free) / 1024**2, 2
                )
        except (OSError, UnicodeError, ValueError):
            pass
    return telemetry


def collect_process_telemetry(pid: int) -> dict[str, Any]:
    try:
        process_group = os.getpgid(pid)
    except (OSError, ProcessLookupError):
        return {}
    text = _safe_command(["ps", "-axo", "pid=,pgid=,%cpu=,rss="])
    return parse_process_group_ps(text, process_group) if text else {}


def collect_telemetry(pid: int) -> dict[str, Any]:
    telemetry: dict[str, Any] = {}
    try:
        telemetry.update(collect_host_telemetry())
    except Exception:
        pass
    try:
        telemetry.update(collect_process_telemetry(pid))
    except Exception:
        pass
    return telemetry

def sample_process_group_rss_mb(process_group: int) -> float | None:
    text = _safe_command(["ps", "-axo", "pid=,pgid=,%cpu=,rss="], timeout=5.0)
    if not text:
        return None
    telemetry = parse_process_group_ps(text, process_group)
    value = telemetry.get("command_rss_mb")
    return float(value) if isinstance(value, (int, float)) else None
