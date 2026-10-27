"""On-demand, read-only Debian Host Ops diagnostics.

Run through the existing Linux Agent bounded command executor:
    PYTHONPATH=platforms/linux/local-agent python -m linux_agent.host_ops capacity
No daemon, scheduler, host selection or periodic resource publication.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import platform
import re
import shutil
import socket
import sys
from typing import Any

from linux_agent.host_ops_git import inspect_git, GitInspectionError
from linux_agent.host_ops_network import probe, NetworkProbeError
from linux_agent.host_ops_ssh import ssh_operation, SSHOperationError
from linux_agent.host_ops_status import inspect_status, StatusInspectionError
from linux_agent.host_ops_artifacts import (
    DEFAULT_MAX_BYTES, inspect_artifact, copy_artifact, ArtifactError,
)

_MEMINFO_MAX_BYTES = 16_384
_TOOL_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,127}$")
_MEM_LINE = re.compile(rb"^(MemTotal|MemAvailable):\s+([0-9]+) kB\s*$")
_MAX_TOOLS = 64
_DEFAULT_TOOLS = ("git", "ssh", "scp", "python3", "sha256sum", "ip", "ss", "lsusb", "adb")


def _require_linux() -> None:
    if platform.system() != "Linux":
        raise ValueError("Linux Host Ops diagnostics require Linux")


def _memory(meminfo_path: Path = Path("/proc/meminfo")) -> dict[str, int | None]:
    """Bounded /proc read. Missing or malformed kernel facts are unknown, never zero."""
    out: dict[str, int | None] = {"MemTotal": None, "MemAvailable": None}
    try:
        with meminfo_path.open("rb") as stream:
            blob = stream.read(_MEMINFO_MAX_BYTES + 1)
    except OSError:
        return out
    if len(blob) > _MEMINFO_MAX_BYTES:
        return out
    seen: set[str] = set()
    for line in blob.splitlines():
        name = line.split(b":", 1)[0]
        if name not in (b"MemTotal", b"MemAvailable"):
            continue
        key = name.decode("ascii")
        if key in seen:
            out[key] = None
            continue
        seen.add(key)
        match = _MEM_LINE.fullmatch(line)
        if match is None:
            continue
        value = int(match.group(2))
        if value <= (2**63 - 1) // 1024:
            out[key] = value * 1024
    return out


def _load() -> list[float | None]:
    try:
        values = os.getloadavg()
    except (OSError, AttributeError):
        return [None, None, None]
    if len(values) != 3 or any(not math.isfinite(v) or v < 0 for v in values):
        return [None, None, None]
    return [round(v, 3) for v in values]


def _storage() -> dict[str, int | None]:
    try:
        disk = shutil.disk_usage("/")
    except OSError:
        return {"total_bytes": None, "free_bytes": None}
    return {"total_bytes": disk.total, "free_bytes": disk.free}


def profile() -> dict[str, Any]:
    _require_linux()
    host = socket.gethostname().strip()
    arch = platform.machine().strip()
    release = platform.release().strip()
    if not host or not arch or not release:
        raise ValueError("missing required kernel host identity")
    return {
        "hostname": host,
        "system": "Linux",
        "release": release,
        "architecture": arch,
        "logical_cpu_count": os.cpu_count(),
        "memory_total_bytes": _memory()["MemTotal"],
        "root": _storage(),
    }


def capacity() -> dict[str, Any]:
    _require_linux()
    return {
        "logical_cpu_count": os.cpu_count(),
        "load_average_1m_5m_15m": _load(),
        "load_average_is_cpu_usage": False,
        "memory_available_bytes": _memory()["MemAvailable"],
        "memory_availability_kind": "linux_memavailable",
        "root": _storage(),
        "admission_slots": None,
    }


def tools(names: tuple[str, ...]) -> dict[str, Any]:
    _require_linux()
    if not 1 <= len(names) <= _MAX_TOOLS:
        raise ValueError("expected 1..64 tool names")
    result = []
    seen = set()
    for name in names:
        if not _TOOL_NAME.fullmatch(name):
            raise ValueError("tool must be an executable basename, not a path")
        if name in seen:
            continue
        seen.add(name)
        located = shutil.which(name)
        result.append({
            "name": name,
            "available": located is not None,
            "path": os.path.realpath(located) if located else None,
            "version": None,
            "version_status": "not_probed",
        })
    return {"items": result, "probe_executed": False}


def inspect(kind: str, names: tuple[str, ...] = (), *,
            max_bytes: int = DEFAULT_MAX_BYTES, timeout: float = 60.0,
            port: int = 22) -> dict[str, Any]:
    payload = {"profile": profile, "capacity": capacity}.get(kind)
    if kind == "tools":
        data = tools(names or _DEFAULT_TOOLS)
    elif kind == "status":
        if len(names) != 1:
            raise ValueError("status requires one explicit private state directory")
        data = inspect_status(names[0])
    elif kind in {"ssh-check", "ssh-exec"}:
        if len(names) < 4 or (kind == "ssh-check" and len(names) != 4):
            raise ValueError("SSH expects HOST USER IDENTITY_FILE KNOWN_HOSTS_FILE [remote argv]")
        data = ssh_operation("check" if kind == "ssh-check" else "exec",
                             names[0], names[1], names[2], names[3],
                             remote_argv=tuple(names[4:]), port=port, timeout=timeout)
    elif kind == "resolve":
        if len(names) != 1:
            raise ValueError("resolve requires one explicit hostname")
        data = probe("resolve", names[0], timeout=min(timeout, 30.0))
    elif kind == "tcp":
        if len(names) != 2:
            raise ValueError("tcp requires exactly HOST PORT")
        try:
            port = int(names[1])
        except ValueError as exc:
            raise ValueError("TCP port must be numeric") from exc
        data = probe("tcp", names[0], port=port, timeout=min(timeout, 30.0))
    elif kind == "git":
        if len(names) != 1:
            raise ValueError("git requires one absolute checkout path")
        data = inspect_git(names[0], timeout=min(timeout, 60.0))
    elif kind == "artifact":
        if len(names) != 1:
            raise ValueError("artifact requires one absolute source path")
        data = inspect_artifact(names[0], max_bytes=max_bytes, timeout=timeout)
    elif kind == "copy":
        if len(names) != 2:
            raise ValueError("copy requires source and destination absolute paths")
        data = copy_artifact(names[0], names[1], max_bytes=max_bytes, timeout=timeout)
    elif payload is not None:
        if names:
            raise ValueError("unexpected positional arguments")
        data = payload()
    elif kind == "overview":
        if names:
            raise ValueError("unexpected positional arguments")
        data = {"profile": profile(), "capacity": capacity()}
    else:
        raise ValueError("unsupported diagnostic action")
    effect_classes = {
        "profile": "passive_read", "capacity": "passive_read",
        "overview": "passive_read", "tools": "passive_read",
        "status": "passive_read", "artifact": "passive_read",
        "git": "local_command_read", "resolve": "active_network_read",
        "tcp": "active_network_read", "ssh-check": "remote_command_read",
        "ssh-exec": "remote_exec", "copy": "local_mutation",
    }
    return {
        "schema_version": 1,
        "kind": kind,
        "observed_at_utc": datetime.now(timezone.utc).isoformat(),
        "effect_class": effect_classes[kind],
        "diagnostic_only": kind not in {"copy", "ssh-exec"},
        "data": data,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="On-demand native Debian Host Ops (no background worker)")
    parser.add_argument("action", choices=("profile", "capacity", "overview", "tools", "git", "artifact", "copy", "resolve", "tcp", "ssh-check", "ssh-exec", "status"))
    parser.add_argument("names", nargs="*")
    parser.add_argument("--format", choices=("json", "text"), default="json")
    parser.add_argument("--max-bytes", type=int, default=DEFAULT_MAX_BYTES)
    parser.add_argument("--timeout", type=float, default=60.0)
    parser.add_argument("--port", type=int, default=22)
    args = parser.parse_args(argv)
    try:
        report = inspect(args.action, tuple(args.names), max_bytes=args.max_bytes,
                         timeout=args.timeout, port=args.port)
    except (ValueError, OSError, ArtifactError, GitInspectionError, NetworkProbeError, SSHOperationError, StatusInspectionError) as exc:
        print(f"linux-host-ops: {exc}", file=sys.stderr)
        return 2
    if args.format == "json":
        print(json.dumps(report, sort_keys=True, ensure_ascii=False, allow_nan=False))
    else:
        print(f"{report['kind']} @ {report['observed_at_utc']} (diagnostic only)")
        for key, value in report["data"].items():
            print(f"{key}: {json.dumps(value, sort_keys=True, ensure_ascii=False)}")
    if report["kind"] in {"ssh-check", "ssh-exec"} and not report["data"]["ok"]:
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
