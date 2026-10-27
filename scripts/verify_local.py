"""Run exact-head local verification without GitHub Actions or network installs.

This entrypoint checks interpreter dependencies before invoking expensive tests,
and records which local checks were actually performed. It does not grant browser
Send or execution authority and never modifies the installed Local Agent daemon.
"""
from __future__ import annotations

import argparse
import importlib.util
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_SHA = re.compile(r"[0-9a-f]{40}\Z")
_LEASE_MARKERS = (
    "LOCAL_AGENT_LEASE_FDS",
    "LOCAL_AGENT_LEASE_KEYS_DIGEST",
    "LOCAL_AGENT_RESOURCE_LEASE_FDS",
)


def exact_head(expected: str, *, root: Path = ROOT) -> str:
    if not _SHA.fullmatch(expected):
        raise ValueError("expected head must be an exact lowercase 40-digit commit SHA")
    actual = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=root,
        check=True, capture_output=True, text=True, timeout=10,
    ).stdout.strip()
    if actual != expected:
        raise ValueError(f"candidate HEAD mismatch: expected {expected}, got {actual}")
    dirty = subprocess.run(
        ["git", "status", "--porcelain", "--untracked-files=normal"],
        cwd=root, check=True, capture_output=True, text=True, timeout=10,
    ).stdout
    if dirty:
        raise ValueError("candidate checkout is not clean; refuse mixed-source tests")
    return actual


_SYSTEM_SSH = Path("/usr/bin/ssh")


def system_openssh_ready() -> bool:
    """Check the pinned real OpenSSH CLI before sandbox integration tests."""
    if not _SYSTEM_SSH.is_file() or not os.access(_SYSTEM_SSH, os.X_OK):
        return False
    try:
        observed = subprocess.run(
            [str(_SYSTEM_SSH), "-V"], capture_output=True, text=True,
            check=False, timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return (observed.returncode == 0
            and (observed.stderr + observed.stdout).strip().startswith("OpenSSH_"))


def missing_dependencies(profile: str, *, include_browser: bool = False,
                         include_python314: bool = False) -> list[str]:
    modules = ["ruff", "mcp", "httpx2"]
    if profile in {"full", "sandbox"}:
        modules += ["pytest", "pytest_cov", "coverage", "hypothesis"]
        if profile == "full" and sys.platform != "darwin":
            modules.append("native macOS host for macos-smoke")
        if profile == "sandbox" and not sys.platform.startswith("linux"):
            modules.append("Linux sandbox host")
    missing = [name for name in modules if (name in {
        "native macOS host for macos-smoke", "Linux sandbox host",
    } or importlib.util.find_spec(name) is None)]
    if profile == "sandbox" and not system_openssh_ready():
        missing.append("real system OpenSSH client (/usr/bin/ssh)")
    if shutil.which("node") is None:
        missing.append("node executable")
    if include_browser:
        # Node has its own package resolution; preflight before Chromium launch.
        if shutil.which("node") is not None:
            try:
                # Browser smokes already accept an isolated absolute module
                # path. Preflight must resolve the very same module.
                module = os.environ.get("LOCAL_AGENT_PLAYWRIGHT_MODULE") or "playwright"
                subprocess.run(
                    ["node", "-e", "require.resolve(process.argv[1])", module],
                    cwd=ROOT, check=True, capture_output=True, timeout=10,
                )
            except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
                missing.append("Node playwright module")
    if include_python314:
        executable = shutil.which("python3.14")
        if executable is None:
            missing.append("python3.14 executable")
        else:
            try:
                subprocess.run([executable, "-c", "import mcp, httpx2"],
                               check=True, capture_output=True, timeout=10)
            except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
                missing.append("Python 3.14 MCP/httpx2 dependencies")
    return missing


def planned_commands(profile: str, *, include_browser: bool = False,
                     include_python314: bool = False,
                     python: str = sys.executable) -> list[tuple[str, list[str]]]:
    stages = [
        ("compile", [python, "scripts/verify.py", "--only", "compile"]),
        ("lint", [python, "scripts/verify.py", "--only", "lint"]),
        ("bridge", [python, "scripts/verify.py", "--only", "bridge"]),
        ("unit and integration", [python, "scripts/verify.py", "--only", "tests"]),
    ]
    if profile in {"full", "sandbox"}:
        if profile == "full":
            stages.append(("macOS smoke", [python, "scripts/verify.py", "--profile", "macos-smoke"]))
        stages.extend([
            ("Host Ops architecture", [python, "scripts/host_ops_quality/check_architecture.py"]),
            ("Host Ops design", [python, "scripts/host_ops_quality/check_design.py"]),
            ("Host Ops coverage", [python, "-m", "pytest", "-q", "--cov=local_agent.host_ops",
                                   "--cov-branch", "--cov-fail-under=85", "host_ops_tests"]),
            ("core coverage", [python, "-m", "coverage", "run", "--source=local_agent", "--omit=*/platforms/macos/local-agent/host_ops/*",
                               "-m", "unittest", "discover", "-q"]),
            ("core coverage report", [python, "-m", "coverage", "report", "--fail-under=70", "-m"]),
        ])
    if include_python314:
        stages.append(("Python 3.14", ["python3.14", "scripts/verify.py", "--only", "tests"]))
    if include_browser:
        stages.append(("isolated Chromium", [python, "scripts/verify.py", "--profile", "bridge-browser"]))
    return stages


def subprocess_environment(*, sanitize_test_lease_markers: bool = False) -> dict[str, str]:
    env = os.environ.copy()
    if sanitize_test_lease_markers:
        # Test subprocesses are not production task/worker descendants. Never
        # apply this option to normal Local Agent command execution.
        for name in _LEASE_MARKERS:
            env.pop(name, None)
    return env


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--expected-sha", required=True)
    parser.add_argument("--profile", choices=("core", "full", "sandbox"), default="core")
    parser.add_argument("--include-browser", action="store_true")
    parser.add_argument("--include-python314", action="store_true")
    parser.add_argument("--sanitize-test-lease-markers", action="store_true",
                        help="Only for hermetic unit tests, never production command execution")
    args = parser.parse_args(argv)
    try:
        head = exact_head(args.expected_sha)
    except (ValueError, OSError, subprocess.SubprocessError) as exc:
        parser.error(str(exc))
    missing = missing_dependencies(args.profile, include_browser=args.include_browser,
                                   include_python314=args.include_python314)
    print(f"local verification HEAD={head} interpreter={sys.executable} profile={args.profile}",
          flush=True)
    if missing:
        print("UNVERIFIED: missing local prerequisites: " + ", ".join(missing), file=sys.stderr)
        return 2
    if args.profile == "full" and sys.platform == "darwin":
        outcome = subprocess.run(
            [sys.executable, "scripts/mac_health.py"], cwd=ROOT, check=False,
        )
        if outcome.returncode:
            print("UNVERIFIED: macOS resource preflight rejected full suite", file=sys.stderr)
            return 2
    if args.profile == "sandbox":
        print("NOT_RUN: native macOS smoke, launchd and live installed Chrome; "
              "Linux source PASS never grants macOS or browser acceptance", flush=True)
    stages = planned_commands(args.profile, include_browser=args.include_browser,
                              include_python314=args.include_python314)
    env = subprocess_environment(sanitize_test_lease_markers=args.sanitize_test_lease_markers)
    if any(os.environ.get(k) for k in _LEASE_MARKERS) and not args.sanitize_test_lease_markers:
        print("WARNING: inherited lease markers may be stale in nested unittest subprocesses",
              file=sys.stderr, flush=True)
    for index, (title, command) in enumerate(stages, 1):
        print(f"[{index}/{len(stages)}] {title}", flush=True)
        started = time.monotonic()
        try:
            result = subprocess.run(command, cwd=ROOT, env=env, check=False)
        except OSError as exc:
            print(f"UNVERIFIED: cannot start {title}: {type(exc).__name__}", file=sys.stderr)
            return 2
        print(f"[{title}] exit={result.returncode} elapsed={time.monotonic()-started:.1f}s",
              flush=True)
        if result.returncode:
            return 1
    print("PASS: all requested local verification stages completed", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
