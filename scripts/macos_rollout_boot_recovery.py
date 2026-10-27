"""Independent *sandbox-only* macOS boot/restart rollback rehearsal.

Manually invoked in tests as if a separate launchd process had started.
Does not install a LaunchAgent, restore a live service or grant authority.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import stat
from typing import Any

from scripts import isolated_rollout_controller as rollout
from scripts import isolated_rollout_fence as fence


def _frozen_marker(root: Path) -> None:
    marker = root / fence._FENCE
    if marker.is_symlink():
        raise rollout.RolloutRejected("admission marker symlink rejected")
    try:
        fd = os.open(marker, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except (FileNotFoundError, IsADirectoryError) as exc:
        raise rollout.RolloutRejected("missing safe admission marker") from exc
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= 256
                or info.st_mode & 0o077 or info.st_uid != os.getuid()):
            raise rollout.RolloutRejected("invalid frozen admission marker")
        with os.fdopen(fd, "rb", closefd=False) as stream:
            content = stream.read(257)
    finally:
        os.close(fd)
    if content != b'{"state":"frozen","sandbox_only":true}\n':
        raise rollout.RolloutRejected("untrusted admission marker contents")


def _unique_pairs(pairs: list[tuple[str, Any]]) -> dict:
    fields: dict[str, Any] = {}
    for key, value in pairs:
        if key in fields:
            raise rollout.RolloutRejected("duplicate recovery journal key")
        fields[key] = value
    return fields


def _read_journal(root: Path) -> dict:
    """Accept only a bounded, regular, nonlinked and uniquely keyed journal."""
    path = root / rollout.JOURNAL
    if path.is_symlink():
        raise rollout.RolloutRejected("recovery journal symlink rejected")
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except (FileNotFoundError, IsADirectoryError) as exc:
        raise rollout.RolloutRejected("missing safe recovery journal") from exc
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= 2 * 1024 * 1024:
            raise rollout.RolloutRejected("invalid recovery journal file")
        with os.fdopen(fd, "rb", closefd=False) as stream:
            raw = stream.read(2 * 1024 * 1024 + 1)
    finally:
        os.close(fd)
    if len(raw) > 2 * 1024 * 1024:
        raise rollout.RolloutRejected("recovery journal exceeded byte limit")
    try:
        data = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_pairs)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise rollout.RolloutRejected("malformed recovery journal") from exc
    if not isinstance(data, dict):
        raise rollout.RolloutRejected("recovery journal root must be an object")
    return data


def recover_after_process_or_boot(root: Path) -> dict:
    """Fail closed on interrupted switch; keep simulated intake disabled.

    The caller supplies no health dict. Boot recovery can roll back only to
    the protected baseline, and cannot silently accept the candidate.
    """
    root = rollout._sandbox(root)
    with fence._gate(root):
        journal = _read_journal(root)
        if journal.get("base_sha") != rollout.BASE_SHA:
            raise rollout.RolloutRejected("invalid recovery baseline revision")
        if journal.get("state") not in {
            "prepared", "armed", "switched_unverified", "accepted", "rolled_back",
        }:
            raise rollout.RolloutRejected("unknown recovery transaction state")
        if journal["state"] != "prepared":
            _frozen_marker(root)
        if journal["state"] in {"armed", "switched_unverified"}:
            outcome = rollout.recover_unverified(root)
        else:
            outcome = journal
        pointer = rollout._pointer(root)
        if outcome["state"] == "rolled_back":
            if pointer != "snapshots/baseline":
                raise rollout.RolloutRejected("rollback pointer is not baseline")
            # An interrupted candidate can be corrupt. A successful rollback
            # must prove the protected baseline, not require the rejected
            # candidate to pass the *same* integrity check as accepted code.
            baseline = root / "snapshots" / "baseline"
            if (rollout._tree_hashes(baseline) != outcome["baseline_hashes"]
                    or rollout._tree_modes(baseline) != outcome["baseline_modes"]):
                raise rollout.RolloutRejected("trusted rollback baseline has changed")
        elif outcome["state"] == "accepted":
            if pointer != "snapshots/candidate":
                raise rollout.RolloutRejected("accepted source pointer changed")
            rollout._verify_snapshots(root, outcome)
        elif outcome["state"] == "prepared":
            if pointer != "snapshots/baseline":
                raise rollout.RolloutRejected("prepared source pointer changed")
            rollout._verify_snapshots(root, outcome)
        # Preserve the frozen marker after rollback or accepted state. A
        # separate approved intake reconciliation must decide when to reopen.
        return {
            "version": 1,
            "boot_simulation": True,
            "transaction_state": outcome["state"],
            "pointer": pointer,
            "admission_frozen": (root / fence._FENCE).exists(),
            "service_restart_performed": False,
            "intake_reopened": False,
            "replay_performed": False,
        }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Offline Mac rollout recovery rehearsal")
    parser.add_argument("sandbox_root", type=Path)
    args = parser.parse_args(argv)
    print(json.dumps(recover_after_process_or_boot(args.sandbox_root), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
