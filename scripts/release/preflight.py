#!/usr/bin/env python3
"""Read-only, fail-closed preflight of local source and operator release receipts.

Never treats unit/source tests as proof of production service acceptance.
No installation, network, state mutation or actual release is performed.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import signal
import stat
import sys
import tarfile
import zipfile
import tempfile
from pathlib import Path
import re
import subprocess

ROOT = Path(__file__).resolve().parents[2]
PLATFORMS = ("macos", "debian", "pi", "termux")
GLOBAL_GATES = ("full_source", "platform_contract", "fault_matrix",
                "g1_no_replay", "g2_stop", "g3_git_recovery",
                "g4_resource_ownership", "supply_chain")
HOST_GATES = ("native_tests", "fresh_install", "rollback", "git_roundtrip")


def git_source(root: Path) -> tuple[str | None, str | None]:
    try:
        head = subprocess.run(
            ["git", "rev-parse", "--verify", "HEAD"], cwd=root,
            capture_output=True, text=True, timeout=10, check=True,
        ).stdout.strip()
        dirty = subprocess.run(
            ["git", "status", "--porcelain", "--untracked-files=all"], cwd=root,
            capture_output=True, text=True, timeout=15, check=True,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return None, None
    return (head, dirty) if re.fullmatch(r"[0-9a-f]{40}", head) else (None, None)


def missing_receipts(receipt: object, source_sha: str,
                     platforms: tuple[str, ...]) -> list[str]:
    if not isinstance(receipt, dict) or receipt.get("schema_version") != 1:
        return ["invalid_receipt_schema"]
    if receipt.get("source_sha") != source_sha:
        return ["receipt_source_sha_mismatch"]
    gates = receipt.get("gates")
    if not isinstance(gates, dict):
        return ["missing_gates"]
    required = (*GLOBAL_GATES, *(f"{platform}.{gate}" for platform in platforms
                                 for gate in HOST_GATES))
    return [
        gate for gate in required
        if (not isinstance(gates.get(gate), dict)
            or gates[gate].get("status") != "PASS"
            or gates[gate].get("source_sha") != source_sha)
    ]



REQUIRED_ARTIFACTS = ("source_bundle", "runtime_sbom",
                      "third_party_notices", "runtime_lock")


SOURCE_BUNDLE_REQUIRED = (
    "AGENTS.md", "README.md", "LICENSE", "requirements-runtime.lock",
    "agentd.py", "agent_entrypoint.py", "agent_parallel.py", "agent_multirepo.py",
    "local_agent/__init__.py",
    "scripts/verify.py", "scripts/release/preflight.py",
    "platforms/macos/local-agent/runtime/task_contract.py",
    "platforms/linux/core/linux_agent/runtime/task_contract.py",
    "platforms/android/local-agent/termux_agent/agent.py",
    "platforms/raspberry-pi/local-agent/pi_agent/__main__.py",
)


def source_bundle_matches_checkout(archive: Path, root: Path) -> bool:
    """Check a tar/zip source archive without extracting it anywhere.

    Accept entries at root or with the exact git archive --prefix=local-agent/.
    Reject special entries and traversal; hash required files against the
    *actual* exact-SHA checkout, not only an operator manifest.
    """
    def safe_name(name: str) -> bool:
        parts = name.rstrip("/").split("/")
        return (name != "" and not name.startswith("/") and "\\" not in name
                and all(part not in ("", ".", "..") for part in parts))

    try:
        if archive.name.endswith(".zip"):
            with zipfile.ZipFile(archive) as bundle:
                entries = {}
                seen = set()
                total = 0
                for item in bundle.infolist():
                    if not safe_name(item.filename) or item.filename in seen or len(seen) >= 20000:
                        return False
                    seen.add(item.filename)
                    mode = item.external_attr >> 16
                    if stat.S_ISLNK(mode) or (mode & 0o170000) not in (0, stat.S_IFREG, stat.S_IFDIR):
                        return False
                    total += item.file_size
                    if item.file_size > 64 * 1024 * 1024 or total > 512 * 1024 * 1024:
                        return False
                    if not item.is_dir():
                        entries[item.filename] = item
                def read_member(name: str):
                    return bundle.open(entries[name])
                return _compare_bundle_members(entries, read_member, root)
        if not archive.name.endswith((".tar", ".tar.gz", ".tgz", ".tar.xz")):
            return False
        with tarfile.open(archive, mode="r:*") as bundle:
            entries = {}
            seen = set()
            total = 0
            for item in bundle.getmembers():
                if not safe_name(item.name) or item.name in seen or len(seen) >= 20000:
                    return False
                seen.add(item.name)
                if not (item.isfile() or item.isdir()):
                    return False
                total += item.size
                if item.size > 64 * 1024 * 1024 or total > 512 * 1024 * 1024:
                    return False
                if item.isfile():
                    entries[item.name] = item
            def read_member(name: str):
                return bundle.extractfile(entries[name])
            return _compare_bundle_members(entries, read_member, root)
    except (OSError, EOFError, ValueError, KeyError, tarfile.TarError, zipfile.BadZipFile):
        return False


SOURCE_RUNTIME_ROOTS = (
    "local_agent",
    "platforms/macos/local-agent",
    "platforms/linux/core",
    "platforms/linux/local-agent/linux_agent",
    "platforms/android/local-agent/termux_agent",
    "platforms/raspberry-pi/local-agent/pi_agent",
    "scripts",
)


def required_bundle_sources(root: Path) -> set[str] | None:
    """Always require the whole local Python engine, not only ten entrypoints."""
    expected = set(SOURCE_BUNDLE_REQUIRED)
    for name in SOURCE_RUNTIME_ROOTS:
        folder = root / name
        if folder.is_symlink():
            return None
        if not folder.is_dir():
            continue
        for path in folder.rglob("*.py"):
            if path.is_symlink():
                return None
            if path.is_file():
                expected.add(path.relative_to(root).as_posix())
                if len(expected) > 20000:
                    return None
    return expected


def _compare_bundle_members(entries: dict, read_member, root: Path) -> bool:
    required = required_bundle_sources(root)
    if required is None:
        return False
    prefixes = ("", "local-agent/")  # no arbitrary nested layout accepted
    if not any(all(prefix + name in entries for name in required)
               for prefix in prefixes):
        return False
    prefix = next(prefix for prefix in prefixes
                  if all(prefix + name in entries for name in required))
    for name in sorted(required):
        local = root / name
        if not local.is_file() or local.is_symlink():
            return False
        stream = read_member(prefix + name)
        if stream is None:
            return False
        with stream as content:
            actual = hashlib.sha256()
            while block := content.read(1024 * 1024):
                actual.update(block)
        expected = hashlib.sha256(local.read_bytes()).hexdigest()
        if actual.hexdigest() != expected:
            return False
    return True


def artifact_gaps(receipt: object, root: Path) -> list[str]:
    """Validate release files by SHA-256, not merely their reported names."""
    if not isinstance(receipt, dict) or not isinstance(receipt.get("artifacts"), dict):
        return list(REQUIRED_ARTIFACTS)
    missing = []
    for name in REQUIRED_ARTIFACTS:
        entry = receipt["artifacts"].get(name)
        if not isinstance(entry, dict):
            missing.append(name)
            continue
        filename, digest = entry.get("path"), entry.get("sha256")
        if (not isinstance(filename, str) or not filename
                or not isinstance(digest, str)
                or not re.fullmatch(r"[a-f0-9]{64}", digest)):
            missing.append(name)
            continue
        path = Path(filename)
        if not path.is_absolute():
            path = root / path
        if (not path.is_file() or path.is_symlink()
                or path.stat().st_size > 1024 * 1024 * 1024):
            missing.append(name)
            continue
        if name == "runtime_lock" and path.resolve() != (root / "requirements-runtime.lock").resolve():
            missing.append(name)
            continue
        hasher = hashlib.sha256()
        try:
            with path.open("rb") as handle:
                for block in iter(lambda: handle.read(1024 * 1024), b""):
                    hasher.update(block)
        except OSError:
            missing.append(name)
            continue
        if (hasher.hexdigest() != digest
                or (name == "source_bundle" and not source_bundle_matches_checkout(path, root))):
            missing.append(name)
    return missing


def inspect(root: Path, platforms: tuple[str, ...],
            evidence: Path | None) -> dict[str, object]:
    failures: list[str] = []
    unknown: list[str] = []
    source_sha, dirty = git_source(root)
    if source_sha is None:
        failures.append("source_not_git_sha")
    if dirty is None or dirty:
        failures.append("source_worktree_not_clean")
    for name in ("LICENSE", "requirements-runtime.lock",
                 "docs/SUPPORT.md", "docs/SECURITY_MODEL.md"):
        path = root / name
        if not path.is_file() or path.is_symlink() or path.stat().st_size == 0:
            failures.append("missing_or_unsafe:" + name)
    if evidence is None:
        unknown.append("missing_operator_release_receipts")
    else:
        try:
            if evidence.is_symlink() or not evidence.is_file():
                raise ValueError("evidence is not a regular file")
            if evidence.stat().st_size > 1024 * 1024:
                raise ValueError("oversized evidence")
            receipt = json.loads(evidence.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, ValueError):
            failures.append("invalid_operator_release_receipts")
        else:
            unknown.extend(missing_receipts(receipt, source_sha or "", platforms))
            unknown.extend("artifact:" + name for name in artifact_gaps(receipt, root))
    status = "FAIL" if failures else ("UNVERIFIED" if unknown else "PASS")
    return {
        "status": status, "source_sha": source_sha, "platforms": list(platforms),
        "failures": failures, "unverified": unknown,
        "operator_receipts_not_independently_verified": True,
        "mutated_source": False,
    }



SOURCE_PROFILES = ("platform-contract", "fault-matrix")


def run_source_gates(root: Path, *, timeout_seconds: int = 1800) -> dict[str, str]:
    """Execute the canonical verify entrypoint, never a second test engine.

    Run only from a clean detached QA checkout. Kill the full subprocess group
    on timeout/interrupt so Git and SIGKILL fixtures cannot remain alive.
    """
    results: dict[str, str] = {}
    for profile in SOURCE_PROFILES:
        with tempfile.TemporaryFile(mode="w+b") as log:
            try:
                child = subprocess.Popen(
                    [sys.executable, "-B", "scripts/verify.py", "--profile", profile],
                    cwd=root, stdout=log, stderr=subprocess.STDOUT,
                    start_new_session=(os.name == "posix"),
                    env={key: value for key, value in os.environ.items()
                         if not key.startswith(("LOCAL_AGENT_LEASE_",
                                                "LOCAL_AGENT_RESOURCE_LEASE_"))},
                )
            except OSError:
                results[profile] = "UNVERIFIED"
                continue
            try:
                returncode = child.wait(timeout=timeout_seconds)
            except BaseException as exc:
                if os.name == "posix":
                    try:
                        os.killpg(child.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                if child.poll() is None:
                    child.kill()
                child.wait()
                if isinstance(exc, subprocess.TimeoutExpired):
                    results[profile] = "UNVERIFIED"
                    continue
                raise
            results[profile] = {0: "PASS", 2: "UNVERIFIED"}.get(returncode, "FAIL")
            if returncode:
                log.seek(0, os.SEEK_END)
                log.seek(max(0, log.tell() - 4096))
                print(f"{profile}: {results[profile]}\n"
                      + log.read().decode("utf-8", errors="replace"), file=sys.stderr)
    return results


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--platform", action="append", choices=PLATFORMS)
    parser.add_argument("--evidence", type=Path)
    parser.add_argument("--run-source-gates", action="store_true",
                        help="run isolated exact-source protocol and fault tests")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    targets = tuple(dict.fromkeys(args.platform or ("macos", "debian")))
    root = args.root.resolve()
    status = inspect(root, targets, args.evidence)
    if args.run_source_gates:
        if status["source_sha"] is None or "source_worktree_not_clean" in status["failures"]:
            status["source_checks"] = {
                profile: "UNVERIFIED" for profile in SOURCE_PROFILES
            }
        else:
            status["source_checks"] = run_source_gates(root)
        if "FAIL" in status["source_checks"].values():
            status["status"] = "FAIL"
        elif "UNVERIFIED" in status["source_checks"].values() and status["status"] != "FAIL":
            status["status"] = "UNVERIFIED"
    if args.json:
        print(json.dumps(status, sort_keys=True, indent=2))
    else:
        print(f"release-preflight: {status['status']} ({status['source_sha']})")
        for issue in status["failures"]:
            print("FAIL " + issue)
        for issue in status["unverified"]:
            print("UNVERIFIED " + issue)
        for profile, outcome in status.get("source_checks", {}).items():
            print(f"{outcome} source gate: {profile}")
    return {"PASS": 0, "FAIL": 1, "UNVERIFIED": 2}[status["status"]]


if __name__ == "__main__":
    raise SystemExit(main())
