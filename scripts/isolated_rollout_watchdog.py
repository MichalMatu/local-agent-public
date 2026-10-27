"""One-shot, separately runnable sandbox rollback watchdog.

No production path, service management, daemon imports or privileged effects.
A real rollout requires an independent OS supervisor beyond this rehearsal.
"""
from __future__ import annotations

import argparse
from pathlib import Path
import time

from scripts import isolated_rollout_controller as transaction


def run_until_deadline(root: Path, *, interval: float = 0.2) -> str:
    if not 0.02 <= interval <= 2:
        raise transaction.RolloutRejected("watchdog interval outside bounds")
    root = transaction._sandbox(root)
    while True:
        # Atomic JSON journal updates permit an independent process to inspect
        # state without coordination with the parent's Python memory.
        state = transaction._journal(root)
        status = state.get("state")
        if status in {"accepted", "rolled_back"}:
            return status
        if status not in {"armed", "switched_unverified"}:
            raise transaction.RolloutRejected("watchdog was not independently armed")
        deadline = state.get("deadline_epoch")
        if isinstance(deadline, bool) or not isinstance(deadline, (int, float)):
            raise transaction.RolloutRejected("invalid watchdog journal deadline")
        left = deadline - time.time()
        if left <= 0:
            # Rollback under its own exclusive transaction lock. A concurrent
            # accepted state cannot be rolled back by recover_unverified().
            return transaction.recover_unverified(root)["state"]
        time.sleep(min(interval, left))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Sandbox-only external rollback decision")
    parser.add_argument("root", type=Path)
    args = parser.parse_args(argv)
    result = run_until_deadline(args.root)
    if result not in {"accepted", "rolled_back"}:
        return 3
    print("ISOLATED_WATCHDOG_" + result.upper(), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
