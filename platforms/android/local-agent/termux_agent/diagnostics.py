"""On-demand, read-only Termux diagnostics; no Git fetch, execution, or repair.

An enabled intake flag is not evidence that runit is running, every spool is
integral, or results reached GitHub. Keep those signals separate.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess
from typing import Any

from linux_agent.agent import Agent

_TASK_ID = re.compile(r"[A-Za-z0-9._-]{1,200}\Z")


def _object_without_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _valid_claim(path: Path) -> bool:
    if path.is_symlink() or not path.is_file() or not _TASK_ID.fullmatch(path.stem):
        return False
    try:
        data = path.read_bytes()
        if len(data) > 4096:
            return False
        claim = json.loads(data, object_pairs_hook=_object_without_duplicates)
    except (OSError, UnicodeError, ValueError):
        return False
    return (isinstance(claim, dict)
            and set(claim) == {"version", "id", "task_digest", "status"}
            and claim["version"] == 1 and claim["id"] == path.stem
            and claim["status"] == "execution_may_have_started"
            and isinstance(claim["task_digest"], str)
            and bool(claim["task_digest"]))


def _bundle_state(root: Path) -> tuple[str, str | None]:
    """Source checkouts have no manifest; installed immutable bundles must."""
    manifest = root / "manifest.json"
    if not manifest.exists():
        return "not_bundled", None
    if manifest.is_symlink() or not manifest.is_file():
        return "invalid", None
    try:
        record = json.loads(manifest.read_bytes(), object_pairs_hook=_object_without_duplicates)
        source = record["source_sha"]
        files = record["files_sha256"]
        if not isinstance(source, str) or not re.fullmatch(r"[0-9a-f]{40}", source):
            return "invalid", None
        if not isinstance(files, dict) or not files:
            return "invalid", source
        for name, expected in files.items():
            rel = Path(name)
            if (rel.is_absolute() or ".." in rel.parts or not isinstance(expected, str)
                    or not re.fullmatch(r"[0-9a-f]{64}", expected)):
                return "invalid", source
            item = root / rel
            if (item.is_symlink() or not item.is_file()
                    or hashlib.sha256(item.read_bytes()).hexdigest() != expected):
                return "invalid", source
    except (OSError, UnicodeError, ValueError, KeyError, TypeError):
        return "invalid", None
    return "verified", source


def _runit_state(state: Path) -> str:
    service = state / "runit/services/termux-agent"
    if not service.exists():
        return "not_configured"
    if service.is_symlink() or not service.is_dir():
        return "invalid"
    binary = shutil.which("sv")
    if binary is None:
        return "unverified"
    try:
        result = subprocess.run([binary, "status", str(service)], timeout=5,
                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                text=True, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return "unverified"
    if result.returncode == 0 and result.stdout.startswith("run:"):
        return "running"
    if result.stdout.startswith("down:"):
        return "down"
    return "unverified"


def diagnose(agent: Agent, *, bundle_root: Path) -> dict[str, Any]:
    """Read-only point-in-time signals. No claims, Git pushes or repairs."""
    state = agent.state
    claim_dir, result_dir = state / "claims", state / "results"
    claims = sorted(claim_dir.glob("*.json"))
    results = sorted(result_dir.glob("*.json"))
    claim_ids = {p.stem for p in claims}
    result_ids = {p.stem for p in results}
    invalid_claims = [p.stem for p in claims if not _valid_claim(p)]
    invalid_spools: list[str] = []
    for path in results:
        try:
            agent._verified_spool_bytes(path)
        except (OSError, UnicodeError, ValueError, RuntimeError):
            invalid_spools.append(path.stem)
    unspooled = sorted(claim_ids - result_ids)
    orphan_spools = sorted(result_ids - claim_ids)
    bundle, source = _bundle_state(bundle_root)
    service = _runit_state(state)
    intake = "disabled" if agent.disabled() else "enabled"
    problem = bool(invalid_claims or invalid_spools or orphan_spools or bundle == "invalid"
                   or service == "invalid")
    status = ("degraded" if problem
              else "attention" if unspooled or service == "down"
              else "unverified" if service in {"unverified", "not_configured"}
              or bundle == "not_bundled" or not claim_dir.is_dir()
              or not result_dir.is_dir()
              else "healthy")
    return {
        "status": status,
        "host_id": agent.config["host_id"],
        "installation_id": agent.config["installation_id"],
        "intake": intake,
        "service": service,
        "bundle": bundle,
        "source_sha": source,
        "claims": len(claims),
        "spooled": len(results),
        "unspooled_claim_ids": unspooled,
        "orphan_spool_ids": orphan_spools,
        "invalid_claim_ids": sorted(invalid_claims),
        "invalid_spool_ids": sorted(invalid_spools),
        "remote_result_publication": "not_checked",
        "no_replay": True,
    }
