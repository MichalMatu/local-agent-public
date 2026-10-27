#!/usr/bin/env python3
"""Opt-in pinned Import Linter, ty and Hypothesis gate; never installs tools.

Must run inside an isolated QA virtualenv against a development checkout.
Missing/wrong-version tools are UNVERIFIED (exit 2), never falsely PASS.
"""
from __future__ import annotations

import argparse
import importlib.metadata
import json
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
VERSIONS = {"import-linter": "2.15", "ty": "0.0.89", "hypothesis": "6.170.0"}
FILES = (
    "platforms/macos/local-agent/repository/checkout_identity.py",
    "platforms/macos/local-agent/repository/onboarding.py",
    "platforms/macos/local-agent/runtime/health_snapshot.py",
    "platforms/macos/local-agent/runtime/backlog_snapshot.py",
)


def verify_environment() -> list[str]:
    issues: list[str] = []
    if sys.prefix == sys.base_prefix:
        issues.append("disposable_virtualenv_required")
    for package, expected in VERSIONS.items():
        try:
            actual = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            issues.append(f"{package}:missing")
        else:
            if actual != expected:
                issues.append(f"{package}:unexpected_version")
    for tool in ("lint-imports", "ty"):
        if not (Path(sys.executable).parent / tool).is_file():
            issues.append(f"{tool}:missing_from_current_virtualenv")
    return issues


def _run_command(command: list[str], root: Path, *, import_view: bool) -> subprocess.CompletedProcess:
    options = dict(capture_output=True, text=True, timeout=150, check=False)
    if not import_view:
        return subprocess.run(command, cwd=root, **options)
    # Static import analyzers cannot follow the public bridge's dynamic __path__.
    # Present the actual package under its public name without adding its
    # internal operator/ directory to Python's startup search path.
    with tempfile.TemporaryDirectory(prefix="la-import-view-") as tmp:
        view = Path(tmp)
        (view / "local_agent").symlink_to(root / "platforms/macos/local-agent",
                                         target_is_directory=True)
        if Path(command[0]).name == "ty":
            command = [*command, "--extra-search-path", str(view)]
        return subprocess.run(command, cwd=view, **options)


def run(root: Path = ROOT) -> dict[str, object]:
    root = root.resolve()
    if not root.is_dir() or not (root / "platforms/macos/local-agent").is_dir():
        return {"status": "unverified", "issues": ["invalid_project_root"]}
    issues = verify_environment()
    if issues:
        return {"status": "unverified", "issues": issues}
    bin_root = Path(sys.executable).parent
    stages = (
        ("imports", [str(bin_root / "lint-imports"),
                     "--config", str(root / "scripts/quality/import_linter_pilot.toml"),
                     "--no-cache"]),
        ("types", [str(bin_root / "ty"), "check", "--no-progress",
                   "--output-format", "concise", *(str(root / path) for path in FILES)]),
        ("properties", [sys.executable, "-m", "unittest", "-q",
                        "scripts.quality.hypothesis_properties"]),
    )
    results: list[dict[str, object]] = []
    for name, command in stages:
        try:
            completed = _run_command(command, root, import_view=name in {"imports", "types"})
        except (OSError, subprocess.TimeoutExpired):
            results.append({"name": name, "status": "unverified",
                            "reason": "launch_error_or_timeout"})
            break
        # Keep private source content and any possible stderr secrets out of
        # task output. The human can rerun the exact CLI locally to debug.
        status = ("pass" if completed.returncode == 0 else
                  "fail" if completed.returncode == 1 else "unverified")
        results.append({"name": name, "status": status,
                        "returncode": completed.returncode,
                        "output_lines": len((completed.stdout + completed.stderr).splitlines())})
        if status != "pass":
            break
    statuses = [item["status"] for item in results]
    final = "fail" if "fail" in statuses else (
        "unverified" if "unverified" in statuses else "pass"
    )
    return {"status": final, "versions": VERSIONS,
            "stages": results, "mutated_project": False,
            "network_or_install_performed": False}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    args = parser.parse_args()
    result = run(args.root)
    print(json.dumps(result, sort_keys=True, indent=2))
    return {"pass": 0, "fail": 1, "unverified": 2}[result["status"]]


if __name__ == "__main__":
    raise SystemExit(main())
