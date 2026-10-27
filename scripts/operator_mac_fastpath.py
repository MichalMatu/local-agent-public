"""Independent macOS owner-only fast-path staging into existing guarded watcher.

Not callable from the Agent's own Git task or daemon. One signed-in Mac
Terminal turn stages exact source+sidecar Python and arms the existing
watcher, leaving the old release/launchd definition for recovery.
"""
from __future__ import annotations

import argparse
import fcntl
import json
import os
from pathlib import Path
import stat
import subprocess
import sys

from scripts import macos_host_self_update as updater
from scripts import macos_host_service as service


def _git(source: Path, *args: str) -> str:
    p = subprocess.run(["/usr/bin/git", "-C", str(source), *args],
                       capture_output=True, text=True, check=False, timeout=20)
    if p.returncode:
        raise updater.HostReleaseBlocked("operator source is not a trusted Git checkout")
    return p.stdout.strip()


def deploy(home: Path, candidate_sha: str, request_id: str) -> dict:
    if (
        sys.platform != "darwin" or os.getuid() != os.geteuid()
        or os.getuid() == 0 or not sys.stdin.isatty()
        or os.getenv("LOCAL_AGENT_LEASE_FDS") or os.getenv("LOCAL_AGENT_LEASE_KEYS_DIGEST")
    ):
        raise updater.HostReleaseBlocked("independent interactive Mac owner terminal required")
    if updater._SHA.fullmatch(candidate_sha) is None or updater._ID.fullmatch(request_id) is None:
        raise updater.HostReleaseBlocked("invalid pinned source/request identity")
    source = Path(__file__).resolve().parents[1]
    if (
        _git(source, "rev-parse", "HEAD") != candidate_sha
        or _git(source, "status", "--porcelain", "--untracked-files=all")
        or _git(source, "remote", "get-url", "origin") not in updater.rollout.SOURCE_ORIGINS
    ):
        raise updater.HostReleaseBlocked("owner operator checkout must be clean exact SHA")
    state = home / "Library/Application Support/local-agent"
    updater._check_directory(state, private=False)
    observed = service.observation(home)
    if not all(observed.get(k) for k in (
        "definition_matches", "revision_matches", "generation_matches", "heartbeat_fresh",
    )) or observed.get("disabled") or observed.get("state") not in ("running", "idle"):
        raise updater.HostReleaseBlocked("current accepted Mac release is not healthy")
    accepted_sha = observed.get("accepted_revision")
    if accepted_sha == candidate_sha:
        return {"status": "already_installed", "sha": candidate_sha}
    if accepted_sha != "2f8bd0b5c19f2c254583379d6a1b05c0d272232f":
        raise updater.HostReleaseBlocked("unexpected accepted baseline; manual review required")
    root = state / "releases" / candidate_sha
    if root.exists() or root.is_symlink():
        raise updater.HostReleaseBlocked("existing candidate release preserved; no repeat")
    intents = state / updater.INTENTS
    completed = state / updater.COMPLETED
    if intents.is_dir():
        updater._check_directory(intents)
        for item in intents.glob("*.json"):
            if not (completed / item.name).is_file():
                raise updater.HostReleaseBlocked("unresolved older update intent")
    watcher_root = home / updater.UPDATER_ROOT
    updater._check_directory(watcher_root)
    lock = watcher_root / "watch.lock"
    if lock.is_symlink():
        raise updater.HostReleaseBlocked("updater lock is symlinked")
    with lock.open("r+") as handle:
        meta = os.fstat(handle.fileno())
        if not stat.S_ISREG(meta.st_mode) or meta.st_uid != os.getuid() or meta.st_mode & 0o077:
            raise updater.HostReleaseBlocked("untrusted updater lock")
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise updater.HostReleaseBlocked("independent updater busy; no new staging") from exc
        # Revalidate the accepted source under external updater ownership.
        check = service.observation(home)
        if check.get("accepted_revision") != accepted_sha or not check.get("heartbeat_fresh"):
            raise updater.HostReleaseBlocked("accepted Mac service changed during admission")
        print("MAC_FASTPATH_BASELINE_VERIFIED=" + accepted_sha, flush=True)
        queued = updater.enqueue_local(home, request_id, candidate_sha)
        if queued.get("status") != "queued_external_controller":
            raise updater.HostReleaseBlocked("external intent was not queued")
        print("MAC_FASTPATH_STAGE_START=" + candidate_sha, flush=True)
        staged = updater.stage(home, request_id, sidecar_python=True)
        print("MAC_FASTPATH_STAGE=" + json.dumps(staged, sort_keys=True), flush=True)
        armed = updater.arm(home, request_id)
        if armed.get("status") != "armed_external_watchdog":
            raise updater.HostReleaseBlocked("independent release watcher not armed")
        print("MAC_FASTPATH_ARM=" + json.dumps(armed, sort_keys=True), flush=True)
        return {
            "status": "external_watchdog_armed_not_installed",
            "candidate_sha": candidate_sha, "rollback_sha": accepted_sha,
            "request_id": request_id,
        }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate-sha", required=True)
    parser.add_argument("--request-id", required=True)
    parser.add_argument("--home", type=Path, default=Path.home())
    args = parser.parse_args(argv)
    try:
        result = deploy(args.home, args.candidate_sha, args.request_id)
    except (updater.HostReleaseBlocked, ValueError, OSError, subprocess.TimeoutExpired) as exc:
        print("MAC_FASTPATH_OPERATOR_REFUSED: " + str(exc), file=sys.stderr)
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
