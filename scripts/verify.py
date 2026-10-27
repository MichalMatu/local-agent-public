#!/usr/bin/env python3
"""Repository verification entrypoint.

Keep command discovery and focused smoke-test selection in one place so CI,
operator docs and local development do not maintain diverging file lists.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# Offline selection for everyday iteration, not a replacement for full/native release gates.
QUICK_TESTS = (
    "tests.test_cli_overview_snapshot",
    "tests.test_cli_overview_human",
    "tests.test_cli_lease_snapshot",
    "tests.test_cli_publication_snapshot",
    "tests.test_cli_queue_snapshot",
    "tests.test_cli_polling_mvp",
    "tests.test_agent_repository",
    "tests.test_agent_repo_admin",
    "tests.test_repository_onboarding_plan",
    "tests.test_repository_registration_status",
    "tests.test_repository_registration_disable",
    "tests.test_repository_registration_detach",
    "tests.test_attached_checkout",
    "tests.test_snapshot_review",
    "tests.test_snapshot_inventory_cli",
    "tests.test_host_remote_grants",
    "tests.test_package_layout",
    "tests.test_verify_quality_entrypoint",
    "tests.test_quality_external_pilot",
    "tests.test_release_preflight",
)


# Explicit, bounded cross-platform *source* smoke; not native device acceptance.
# Full platform tests are still run in disposable exact-SHA checkouts on each OS.
PLATFORM_SOURCE_SMOKE_TESTS = (
    ("Linux core imports", "platforms/linux/local-agent/tests",
     "test_linux_shared_core_layout.py",
     ("platforms/linux/local-agent", "platforms/linux/core")),
    ("Linux Git result publisher", "platforms/linux/local-agent/tests",
     "test_linux_project_publisher.py",
     ("platforms/linux/local-agent", "platforms/linux/core")),
    ("Raspberry Pi source port", "platforms/raspberry-pi/local-agent/tests",
     "test_pi_port.py",
     ("platforms/raspberry-pi/local-agent", "platforms/linux/local-agent",
      "platforms/linux/core")),
    ("Termux Git and lifecycle contract", "platforms/android/local-agent/tests",
     "test_termux_agent.py",
     ("platforms/android/local-agent", "platforms/linux/local-agent",
      "platforms/linux/core")),
    ("Termux independent release operator", "platforms/android/local-agent/tests",
     "test_termux_fastpath_operator.py",
     ("platforms/android/local-agent", "platforms/linux/local-agent",
      "platforms/linux/core")),
)


MACOS_SMOKE_TESTS = (
    "tests.test_package_layout",
    "tests.test_release_hardening",
    "tests.test_guard_process",
    "tests.test_lease_recovery",
    "tests.test_agent_multirepo_restart",
    "tests.test_agentd_dispatch",
    "tests.test_self_update_environment",
    "tests.test_macos_launchd",
    "tests.test_remote_operator",
    "tests.test_agent_process",
    "tests.test_agent_core",
    "tests.test_agent_runtime",
    "tests.test_operator_observability",
    "tests.test_agent_storage",
    "tests.test_agent_binding",
    "tests.test_serial_agent_binding",
    "tests.test_runtime_admission_hardening",
    "tests.test_runtime_admission_matrix",
    "tests.test_agent_repo_worker",
    "tests.test_agent_parallel",
    "tests.test_agent_parallel_worker",
    "tests.test_parallel_ingestion_dedupe",
    "tests.test_parallel_control",
    "tests.test_parallel_process",
    "tests.test_multirepo_integration",
    "tests.test_parallel_integration",
    "tests.test_control_admission_policy",
    "tests.test_control_probe_parallel_admission",
    "tests.test_stale_cancel_self_update",
    "tests.test_current_documentation_contract",
    "tests.test_supervisor_architecture_contract",
    "tests.test_parallel_resource_wait",
    "tests.test_control_hardening",
    "tests.test_supervisor_modules",
    "tests.test_emergency_controls",
    "tests.test_entrypoint_guard",
    "tests.test_mcp_registry",
    "tests.test_mcp_client",
    "tests.test_mcp_cli",
)


def _display_command(command: list[str]) -> str:
    """Keep the terminal report readable without changing execution arguments."""
    printable = " ".join(command)
    if len(printable) <= 240:
        return printable
    prefix = " ".join(command[:6])
    return f"{prefix} ... ({len(command) - 6} additional arguments, all executed)"


def _verification_environment() -> dict[str, str]:
    """Do not propagate already-closed parent lease FDs into QA subprocesses.

    A live valid lease is kept intact. Only this offline verifier cleans a
    stale inherited descriptor from test process environment. The production
    task executor's strict lease-fd validation remains unchanged.
    """
    env = dict(os.environ)
    for name in ("LOCAL_AGENT_LEASE_FDS", "LOCAL_AGENT_RESOURCE_LEASE_FDS"):
        raw = env.get(name, "").strip()
        if not raw:
            continue
        try:
            fds = tuple(int(item) for item in raw.split(","))
        except ValueError:
            continue  # Malformed values still fail in the actual executor.
        if not fds or any(fd < 3 for fd in fds):
            continue
        try:
            for fd in fds:
                os.fstat(fd)
        except OSError:
            # The parent already closed this FD; no lease can be inherited.
            # Only source verification subprocesses receive the cleaned env.
            env.pop(name, None)
            print(f"verify: ignored stale inherited {name}", flush=True)
    return env


def _run(
    label: str, command: list[str], *, env_overrides: dict[str, str] | None = None,
) -> None:
    print(f"\n==> {label}\n$ {_display_command(command)}", flush=True)
    environment = _verification_environment()
    if env_overrides:
        environment.update(env_overrides)
    # A valid inherited lease must include its actual open descriptor in the
    # child, not just a string naming an FD subprocess.run would close.
    pass_fds: set[int] = set()
    for name in ("LOCAL_AGENT_LEASE_FDS", "LOCAL_AGENT_RESOURCE_LEASE_FDS"):
        raw = environment.get(name, "").strip()
        if raw:
            try:
                pass_fds.update(int(item) for item in raw.split(","))
            except ValueError:
                # Malformed parent lease environment still fails in the
                # existing strict child validator; do not invent a lease.
                pass
    subprocess.run(
        command, cwd=ROOT, check=True, env=environment,
        pass_fds=tuple(sorted(pass_fds)),
    )


def _require(executable: str) -> str:
    resolved = shutil.which(executable)
    if resolved is None:
        raise SystemExit(f"required executable not found on PATH: {executable}")
    return resolved


def _python_sources() -> list[str]:
    roots = sorted(
        path.relative_to(ROOT).as_posix()
        for path in ROOT.glob("*.py")
        if path.name.startswith("agent")
    )
    # Keep source syntax verification aligned with the physical host matrix.
    # Runtime package tests have their own scoped unittest discovery.
    package_roots = (
        "platforms/macos/local-agent",
        "platforms/macos/installer",
        "platforms/linux/core",
        "platforms/linux/local-agent",
        "platforms/raspberry-pi/local-agent",
        "platforms/android/local-agent",
    )
    packages = sorted(
        path.relative_to(ROOT).as_posix()
        for relative in package_roots
        for path in (ROOT / relative).rglob("*.py")
        if "tests" not in path.relative_to(ROOT / relative).parts
    )
    scripts = sorted(
        path.relative_to(ROOT).as_posix()
        for path in (ROOT / "scripts").rglob("*.py")
    )
    return sorted(set(roots + packages + scripts))


def compile_sources() -> None:
    sources = _python_sources()
    if not sources:
        raise SystemExit("no Python sources discovered for compile verification")
    _run("Compile Python sources", [sys.executable, "-m", "py_compile", *sources])


def lint_sources() -> None:
    ruff = _require("ruff")
    _run("Ruff", [ruff, "check", "."])


def quality_guardrails() -> None:
    """Cheap offline drift checks before slow/native tests."""
    _run("Documentation/ownership/size/import guards", [
        sys.executable, "scripts/quality/check.py",
    ])
    _run("Quality guard and canonical docs regressions", [
        sys.executable, "-m", "unittest", "-q",
        "tests.test_quality_gates", "tests.test_current_documentation_contract",
        "tests.test_handoff_generator",
    ])
    # Review branches may run source QA; canonical handoff still requires --check.
    _run("Exact-SHA source/document contract (review branch allowed)", [
        sys.executable, "scripts/handoff.py", "--check-source",
    ])


def validate_bridge() -> None:
    node = _require("node")
    bridge_dir = ROOT / "chat_bridge"

    javascript = sorted(bridge_dir.glob("*.js"))
    for path in javascript:
        _run(
            f"Node syntax: {path.name}",
            [node, "--check", path.relative_to(ROOT).as_posix()],
        )

    for path in sorted(bridge_dir.glob("*.test.js")):
        _run(
            f"Node test: {path.name}",
            [node, path.relative_to(ROOT).as_posix()],
        )

    json_files = sorted(bridge_dir.glob("*.json")) + sorted((ROOT / "config").glob("*.json"))
    for path in json_files:
        with path.open("r", encoding="utf-8") as handle:
            json.load(handle)
        print(f"validated JSON: {path.relative_to(ROOT)}")


def run_tests() -> None:
    _run("Python unit and integration tests", [sys.executable, "-m", "unittest", "discover", "-q"])


def run_platform_source_smoke() -> None:
    """Deterministic offline subset across accepted OS sources, not host E2E."""
    for label, directory, pattern, python_roots in PLATFORM_SOURCE_SMOKE_TESTS:
        test_file = ROOT / directory / pattern
        if not test_file.is_file() or test_file.is_symlink():
            raise SystemExit(f"missing platform source smoke test: {test_file}")
        paths = tuple(ROOT / relative for relative in python_roots)
        if any(not path.is_dir() or path.is_symlink() for path in paths):
            raise SystemExit(f"missing or unsafe platform source root: {label}")
        _run(
            label,
            [sys.executable, "-m", "unittest", "discover", "-s", directory,
             "-p", pattern, "-q"],
            env_overrides={"PYTHONPATH": os.pathsep.join(str(path) for path in paths)},
        )


def run_platform_contract() -> None:
    """Four independent source-import contracts, not native device acceptance."""
    _run("Four-platform wire contract", [
        sys.executable, "-m", "unittest", "-q",
        "tests.test_platform_contract_matrix",
    ])


def run_fault_matrix() -> None:
    """Opt-in selected isolated Git, STOP and SIGKILL regressions."""
    _run("Offline fault-injection matrix", [
        sys.executable, "scripts/quality/fault_matrix.py",
    ])


def run_quick() -> None:
    """Bounded local preflight without device effects, CI or installation."""
    quality_guardrails()
    compile_sources()
    lint_sources()
    _run(
        "Focused repo/host safety regression",
        [sys.executable, "-m", "unittest", "-q", *QUICK_TESTS],
    )


def run_deep() -> None:
    """Explicit opt-in offline architecture, fault and supply-chain suite.

    OpenVisio, Import Linter, ty, Vulture, and pip-audit are separate
    optional external pilots, not silently assumed to be installed or run.
    """
    run_quick()
    _run(
        "Python architecture graph/cycles/hotspots (observation, not a clean-graph claim)",
        [sys.executable, "scripts/quality/architecture_report.py"],
    )
    _run(
        "Seeded malformed task/JSON and security scan regression",
        [sys.executable, "-m", "unittest", "-q",
         "tests.test_quality_architecture_report",
         "tests.test_quality_contract_fuzz",
         "tests.test_quality_security_audit",
         "tests.test_fault_matrix_runner"],
    )
    _run(
        "Offline Gitleaks and strict direct dependency manifest audit",
        [sys.executable, "scripts/quality/security_audit.py"],
    )


def run_external() -> None:
    """Pinned, already-installed optional tools only; never install into Mac service."""
    _run(
        "Verified optional Import Linter, ty and Hypothesis pilot",
        [sys.executable, "scripts/quality/external_pilot.py"],
    )


def run_macos_smoke() -> None:
    check = subprocess.run(
        [sys.executable, "scripts/mac_health.py"], cwd=ROOT, check=False,
    )
    if check.returncode:
        raise SystemExit(2)  # UNVERIFIED host pressure, not source test FAIL.
    compile_sources()
    quality_guardrails()
    _run(
        "macOS focused smoke suite",
        [sys.executable, "-m", "unittest", "-q", *MACOS_SMOKE_TESTS],
    )


def run_bridge_browser() -> None:
    """Isolated Chromium load/diagnostic only; never submit a real prompt."""
    node = _require("node")
    _run("GitHub-first thin Bridge isolated Chromium smoke", [node, "scripts/github_first_browser_smoke.cjs"])


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--only",
        choices=("compile", "lint", "quality", "bridge", "tests"),
        help="run one verification stage instead of the full suite",
    )
    parser.add_argument(
        "--profile",
        choices=("quick", "deep", "external", "full", "macos-smoke",
                 "bridge-browser", "platform-source-smoke",
                 "platform-contract", "fault-matrix"),
        default="full",
        help="verification profile; default: full",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    if args.only:
        stages = {
            "compile": compile_sources,
            "lint": lint_sources,
            "quality": quality_guardrails,
            "bridge": validate_bridge,
            "tests": run_tests,
        }
        stages[args.only]()
        return 0

    if args.profile == "quick":
        run_quick()
        return 0
    if args.profile == "deep":
        run_deep()
        return 0
    if args.profile == "external":
        run_external()
        return 0
    if args.profile == "macos-smoke":
        run_macos_smoke()
        return 0
    if args.profile == "bridge-browser":
        run_bridge_browser()
        return 0
    if args.profile == "platform-contract":
        run_platform_contract()
        return 0
    if args.profile == "fault-matrix":
        run_fault_matrix()
        return 0
    if args.profile == "platform-source-smoke":
        run_platform_source_smoke()
        return 0

    compile_sources()
    lint_sources()
    quality_guardrails()
    validate_bridge()
    run_tests()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
