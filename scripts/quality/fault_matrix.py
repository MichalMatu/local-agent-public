#!/usr/bin/env python3
"""Run a bounded, isolated selection of existing real Git/STOP/crash regressions.

No live Agent, GitHub repository, device, installer or user checkout is touched.
Missing test/discovery or timed-out execution is UNVERIFIED, never PASS.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[2]
LINUX = ("platforms/linux/local-agent", "platforms/linux/core")
TERMUX = ("platforms/android/local-agent", *LINUX)

# (name, test directory, file, exact unittest method, import roots)
CASES = (
    ("linux-sigkill", "platforms/linux/local-agent/tests", "test_linux_git_pilot.py",
     "test_real_worker_sigkill_recovers_orphan_without_replay", LINUX),
    ("linux-lost-ack", "platforms/linux/local-agent/tests", "test_linux_git_pilot.py",
     "test_lost_push_ack_confirms_original_result_without_second_effect", LINUX),
    ("linux-outage", "platforms/linux/local-agent/tests", "test_linux_git_pilot.py",
     "test_publication_outage_no_replay", LINUX),
    ("linux-stop", "platforms/linux/local-agent/tests", "test_linux_git_pilot.py",
     "test_stop_between_stages_fences_next_command_before_spawn", LINUX),
    ("linux-corrupt-spool", "platforms/linux/local-agent/tests", "test_linux_git_pilot.py",
     "test_corrupt_durable_spool_never_published_or_reexecuted", LINUX),
    ("termux-stop", "platforms/android/local-agent/tests", "test_termux_agent.py",
     "test_stop_between_stages_fences_termux_command_before_spawn", TERMUX),
    ("termux-outage", "platforms/android/local-agent/tests", "test_termux_agent.py",
     "test_git_publication_outage_preserves_spool_and_no_replay", TERMUX),
    ("macos-lost-ack", "tests", "test_host_remote_git_e2e.py",
     "test_lost_push_ack_does_not_requeue_a_remotely_published_result", ()),
    ("macos-rebase-gc", "tests", "test_control_rebase_guard.py",
     "test_managed_interrupted_publication_rebuilds_without_erasing_execution_ledger", ()),
    ("macos-rebase-gc-negative", "tests", "test_control_rebase_guard.py",
     "test_gc_recovery_only_accepts_flat_bounded_json_task_deletions", ()),
    ("macos-foreign-host", "tests", "test_host_remote_worker.py",
     "test_discovery_skips_foreign_installations_without_claiming", ()),
    ("macos-cas-retry", "tests", "test_control_publication.py",
     "test_git_compare_and_swap_push_race_is_reconciled_in_turn", ()),
    ("macos-control-timeout", "tests", "test_git_network_resilience.py",
     "test_control_sync_gives_only_a_timed_out_attempt_a_longer_retry", ()),
)


def _terminate_test_group(process: subprocess.Popen) -> None:
    """Bound descendants too: a unittest timeout must not orphan Git workers."""
    if os.name == "posix":
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    if process.poll() is None:
        process.kill()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        # The test group is still unverified; never claim a successful cleanup.
        process.kill()


def execute(case: tuple, root: Path = ROOT, timeout: int = 180) -> dict:
    name, directory, file, method, imports = case
    test = root / directory / file
    if not test.is_file() or test.is_symlink():
        return {"case": name, "status": "UNVERIFIED", "reason": "missing_test"}
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join([str(root), *(str(root / p) for p in imports)])
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    for key in tuple(env):
        if key.startswith(("LOCAL_AGENT_LEASE_", "LOCAL_AGENT_RESOURCE_LEASE_")):
            env.pop(key)
    args = [sys.executable, "-B", "-m", "unittest", "discover", "-s", directory,
            "-p", file, "-k", method, "-q"]
    # A disposable disk-backed log avoids an unbounded stdout/stderr pipe.
    with tempfile.TemporaryFile(mode="w+b") as output:
        try:
            process = subprocess.Popen(
                args, cwd=root, env=env, stdout=output, stderr=subprocess.STDOUT,
                start_new_session=(os.name == "posix"),
            )
        except OSError:
            return {"case": name, "status": "UNVERIFIED", "reason": "runner_unavailable"}
        try:
            returncode = process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            _terminate_test_group(process)
            return {"case": name, "status": "UNVERIFIED", "reason": "timeout_group_killed"}
        except BaseException:
            # Cancellation must never abandon a detached fault-test process.
            _terminate_test_group(process)
            raise
        # unittest's final count is at the end, even when an earlier case logs.
        output.seek(0, os.SEEK_END)
        total_bytes = output.tell()
        output.seek(max(0, total_bytes - 65536))
        tail = output.read().decode("utf-8", errors="replace")
    matches = re.findall(r"\bRan (\d+) tests? in\b", tail)
    if len(matches) != 1 or int(matches[0]) != 1:
        return {"case": name, "status": "UNVERIFIED",
                "reason": "test_discovery_mismatch", "returncode": returncode,
                "output_tail": tail[-4096:]}
    if returncode == 0 and not re.search(r"^OK\s*$", tail, re.MULTILINE):
        return {"case": name, "status": "UNVERIFIED",
                "reason": "test_not_successfully_exercised", "returncode": returncode,
                "output_tail": tail[-4096:]}
    report = {"case": name, "status": "PASS" if returncode == 0 else "FAIL",
              "returncode": returncode, "tests": 1}
    if returncode:
        report["output_tail"] = tail[-4096:]
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--case", action="append", choices=[c[0] for c in CASES])
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    selected = [c for c in CASES if args.case is None or c[0] in args.case]
    outcomes = [execute(case) for case in selected]
    states = {item["status"] for item in outcomes}
    status = "FAIL" if "FAIL" in states else (
        "UNVERIFIED" if "UNVERIFIED" in states else "PASS"
    )
    result = {"status": status, "source_only": True, "cases": outcomes}
    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        for item in outcomes:
            print(f"{item['case']}: {item['status']}")
        print(f"fault-matrix: {status}")
    return {"PASS": 0, "FAIL": 1, "UNVERIFIED": 2}[status]


if __name__ == "__main__":
    raise SystemExit(main())
