#!/usr/bin/env python3
"""Offline, read-only secret and pinned-dependency audit.

Never emit credential values, scanned lines or raw scanner findings.
Third-party CLI availability is explicit: missing is UNVERIFIED (exit 2).
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MANIFESTS = (
    "requirements-runtime.txt",
    "requirements-desktop.txt",
    "requirements-desktop-build.txt",
    "requirements-host-ops-browser.txt",
    "scripts/sandbox/requirements-linux-py313.txt",
)
PIN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*(?:\[[A-Za-z0-9_,.-]+\])?==[A-Za-z0-9][A-Za-z0-9_.+!-]*$")


def manifest_report(root: Path) -> dict[str, object]:
    issues: list[str] = []
    total = 0
    for name in MANIFESTS:
        path = root / name
        if not path.is_file() or path.is_symlink():
            issues.append(f"{name}: missing or linked dependency manifest")
            continue
        for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            value = line.split("#", 1)[0].strip()
            if not value:
                continue
            total += 1
            if not PIN.fullmatch(value):
                # Do not print an unpinned URL or possible embedded credentials.
                issues.append(f"{name}:{line_number}: dependency is not a strict == pin")
    return {
        "status": "fail" if issues else "pass",
        "manifests": len(MANIFESTS),
        "direct_dependencies": total,
        "issues": issues,
        "vulnerability_database_checked": False,
    }


def history_preflight(root: Path) -> str | None:
    """Require the repository root and complete local history before scanning."""
    for args, expected in (
        (("rev-parse", "--show-toplevel"), str(root.resolve())),
        (("rev-parse", "--is-shallow-repository"), "false"),
    ):
        try:
            done = subprocess.run(
                ["git", "-C", str(root), *args],
                capture_output=True, text=True, check=False, timeout=10,
            )
        except (OSError, subprocess.TimeoutExpired):
            return "git_history_unavailable"
        if done.returncode != 0:
            return "git_history_unavailable"
        if done.stdout.strip() != expected:
            return "not_repository_root" if args[-1] == "--show-toplevel" else "shallow_history"
    return None


def secrets_report(
    root: Path, *, command: str | None = None, history: bool = False,
) -> dict[str, object]:
    executable = command or shutil.which("gitleaks")
    if not executable:
        return {
            "status": "unverified",
            "scanner": "gitleaks",
            "reason": "scanner_not_installed",
            "findings": None,
        }
    if history:
        problem = history_preflight(root)
        if problem is not None:
            return {
                "status": "unverified", "scanner": "gitleaks",
                "reason": problem, "findings": None,
            }
    with tempfile.TemporaryDirectory(prefix="la-gitleaks-redacted-") as tmp:
        report_path = Path(tmp) / "findings.json"
        args = [
            executable, "git" if history else "dir",
            "--no-banner", "--no-color", "--redact=100",
            "--report-format=json", "--report-path", str(report_path),
            "--max-archive-depth=0", "--max-target-megabytes=10",
            # cwd=root gives stable repository-relative fingerprints so a
            # single reviewed .gitleaksignore entry works across temp clones.
            "--timeout=90",
            *(["--log-opts=--all --full-history"] if history else []),
            ".",
        ]
        try:
            done = subprocess.run(
                args, cwd=root, capture_output=True, timeout=105,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return {
                "status": "unverified", "scanner": "gitleaks",
                "reason": "execution_error_or_timeout", "findings": None,
            }
        if done.returncode not in (0, 1):
            return {
                "status": "unverified", "scanner": "gitleaks",
                "reason": "unexpected_exit_code", "findings": None,
            }
        if not report_path.is_file():
            if done.returncode == 1:
                return {
                    "status": "unverified", "scanner": "gitleaks",
                    "reason": "findings_without_report", "findings": None,
                }
            return {"status": "pass", "scanner": "gitleaks", "findings": 0}
        try:
            report = json.loads(report_path.read_text(encoding="utf-8"))
        except (ValueError, UnicodeError, OSError):
            return {
                "status": "unverified", "scanner": "gitleaks",
                "reason": "invalid_report", "findings": None,
            }
        if not isinstance(report, list):
            return {
                "status": "unverified", "scanner": "gitleaks",
                "reason": "invalid_report", "findings": None,
            }
        if (done.returncode == 0 and report) or (done.returncode == 1 and not report):
            return {
                "status": "unverified", "scanner": "gitleaks",
                "reason": "exit_report_mismatch", "findings": None,
            }
        # Report content is intentionally never printed, archived or uploaded.
        return {
            "status": "fail" if report else "pass",
            "scanner": "gitleaks",
            "findings": len(report),
        }


def run(root: Path, *, mode: str = "all", command: str | None = None) -> dict[str, object]:
    if not root.is_dir() or root.is_symlink() or not (root / "local_agent").is_dir():
        return {"status": "unverified", "reason": "invalid_source_root"}
    checks: dict[str, object] = {}
    if mode in {"all", "manifests"}:
        checks["manifests"] = manifest_report(root)
    if mode in {"all", "secrets"}:
        checks["secrets"] = secrets_report(root, command=command)
    if mode in {"all", "history"}:
        checks["history"] = secrets_report(root, command=command, history=True)
    states = [item["status"] for item in checks.values()]
    status = "fail" if "fail" in states else (
        "unverified" if "unverified" in states else "pass"
    )
    return {"status": status, "checks": checks, "mutated_source": False}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--mode", choices=("all", "manifests", "secrets", "history"), default="all")
    args = parser.parse_args()
    result = run(args.root.resolve(), mode=args.mode)
    print(json.dumps(result, indent=2, sort_keys=True))
    return {"pass": 0, "fail": 1, "unverified": 2}[result["status"]]


if __name__ == "__main__":
    raise SystemExit(main())
