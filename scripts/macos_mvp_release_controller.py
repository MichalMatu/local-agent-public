"""One-shot independent macOS MVP cutover and fail-closed B1c rollback.

The source is copied outside the candidate checkout and runs as a dedicated
launchd job, never inside the Local Agent worker it restarts. No second task
transport/worker, root helper, per-command grants, or operator ref edits.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import plistlib
import subprocess
import sys
import time
import tempfile

BASE_SHA = "1df07c5160269f45f774c6d0144ed2a3b971f52e"
CANDIDATE_SHA = "7df83dcd998e23b30a5c18ed6d347772aa3e5d98"
RELEASE_NAME = "local-agent-host-release-20261018-7df83dcd"
HOST_ID = "example-mac"
INSTALL_ID = "e6610a5b-cb49-4027-83f2-2dcfdc4a66be"
LABEL = "com.michal.local-agent"
JOURNAL = "mvp-cutover.json"
FROZEN = "macos-release-admission-frozen.json"
LOCK = "macos-release-admission.lock"
CONTROLLER_LABEL = "dev.localagent.mvp-cutover.7df83dcd"


class CutoverBlocked(RuntimeError):
    """Safety admission or recovery verification failed."""


def _atomic(path: Path, data: bytes) -> None:
    fd, name = tempfile.mkstemp(prefix="." + path.name + ".", dir=path.parent)
    tmp = Path(name)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
        parent = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(parent)
        finally:
            os.close(parent)
    finally:
        tmp.unlink(missing_ok=True)


def _json(path: Path) -> dict:
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 8192:
        raise CutoverBlocked("missing/invalid release record")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise CutoverBlocked("invalid release record")
    return payload


def _git(path: Path, *argv: str) -> str:
    p = subprocess.run(["/usr/bin/git", "-C", str(path), *argv],
                       capture_output=True, text=True, timeout=35)
    if p.returncode:
        raise CutoverBlocked("release source Git identity invalid")
    return p.stdout.strip()


def _validate_paths(home: Path, *, require_candidate: bool = True) -> tuple[Path, Path, Path, Path]:
    if sys.platform != "darwin" or os.geteuid() != os.getuid():
        raise CutoverBlocked("requires non-root real macOS user")
    if not home.is_absolute() or home.is_symlink() or not home.is_dir():
        raise CutoverBlocked("invalid Mac home")
    root = home / RELEASE_NAME
    if root.is_symlink() or not root.is_dir():
        raise CutoverBlocked("missing staged release")
    meta = root.stat()
    if meta.st_uid != os.getuid() or meta.st_mode & 0o077:
        raise CutoverBlocked("staging root not private to service user")
    manifest = _json(root / "staging-manifest.json")
    if (manifest.get("status") != "staged_not_activated"
            or manifest.get("baseline_sha") != BASE_SHA
            or manifest.get("candidate_sha") != CANDIDATE_SHA
            or manifest.get("host_id") != HOST_ID
            or manifest.get("installation_id") != INSTALL_ID):
        raise CutoverBlocked("staged release identity mismatch")
    baseline = root / "baseline-B1c"
    candidate = root / "candidate-7df83dcd"
    sources = ((baseline, BASE_SHA), (candidate, CANDIDATE_SHA)) if require_candidate else ((baseline, BASE_SHA),)
    for path, revision in sources:
        if path.is_symlink() or not path.is_dir() or not (path / "agent_entrypoint.py").is_file():
            raise CutoverBlocked("staged release checkout unavailable")
        if _git(path, "rev-parse", "HEAD") != revision or _git(path, "status", "--porcelain"):
            raise CutoverBlocked("staged release source drift")
    state = home / "Library/Application Support/local-agent"
    identity = _json(state / "host-identity.json")
    if identity.get("host_id") != HOST_ID or identity.get("installation_id") != INSTALL_ID:
        raise CutoverBlocked("wrong host installation")
    plist_path = home / "Library/LaunchAgents" / (LABEL + ".plist")
    return root, state, candidate, plist_path


def _load_state(root: Path) -> dict:
    return _json(root / JOURNAL)


def _save_state(root: Path, payload: dict, status: str) -> dict:
    value = dict(payload, state=status)
    _atomic(root / JOURNAL, (json.dumps(value, sort_keys=True) + "\n").encode())
    return value


def _launchctl(*argv: str, acceptable: tuple[int, ...] = (0,)) -> subprocess.CompletedProcess[str]:
    r = subprocess.run(["/bin/launchctl", *argv], capture_output=True,
                       text=True, timeout=18)
    if r.returncode not in acceptable:
        raise CutoverBlocked("launchd operation failed")
    return r


def _generation(target: str, base: str, candidate: str) -> str:
    p = _launchctl("print", target, acceptable=(0, 113))
    if p.returncode == 113:
        return "absent"
    has_base = base in p.stdout
    has_candidate = candidate in p.stdout
    if has_base == has_candidate:
        raise CutoverBlocked("unrecognized or ambiguous launchd generation")
    return "baseline" if has_base else "candidate"


def _wait_until_absent(target: str, baseline: str, candidate: str) -> None:
    """launchctl bootout is not a barrier: wait for complete deregistration."""
    deadline = time.monotonic() + 20
    while True:
        if _generation(target, baseline, candidate) == "absent":
            return
        if time.monotonic() >= deadline:
            raise CutoverBlocked("launchd service remained registered after bootout")
        time.sleep(0.2)


def _bootout_and_wait(target: str, baseline: str, candidate: str) -> None:
    _launchctl("bootout", target)
    _wait_until_absent(target, baseline, candidate)


def _bootstrap_with_retry(
    domain: str, plist: Path, target: str, *,
    expected: str, baseline: str, candidate: str,
) -> None:
    """Bound the macOS bootstrap error-5/unload race without losing identity.

    A nonzero bootstrap can have succeeded despite its status; inspect the
    registered source before retrying. Never replace or bootout an unexpected
    generation, and never report success from bootstrap exit code alone.
    """
    for attempt in range(1, 5):
        result = _launchctl("bootstrap", domain, str(plist), acceptable=(0, 5))
        deadline = time.monotonic() + 3
        while True:
            actual = _generation(target, baseline, candidate)
            if actual == expected:
                return
            if actual != "absent":
                raise CutoverBlocked("launchd has an unexpected generation after bootstrap")
            if time.monotonic() >= deadline:
                break
            time.sleep(0.2)
        if result.returncode not in (0, 5):
            raise CutoverBlocked("launchd bootstrap rejected the release")
        if attempt != 4:
            time.sleep(0.5 * attempt)
    raise CutoverBlocked("launchd bootstrap did not register the expected service after retries")


def _wait_healthy_baseline(
    state: Path, target: str, baseline: str, candidate: str,
) -> None:
    """Never label rollback complete from a registered but dead launchd job.

    Require the original entrypoint to be running and its own local
    supervisor heartbeat to remain fresh over a short stability window.
    """
    deadline = time.monotonic() + 35
    stable_at: float | None = None
    while time.monotonic() < deadline:
        process = _launchctl("print", target, acceptable=(0, 113))
        healthy = False
        if (process.returncode == 0
                and baseline in process.stdout and candidate not in process.stdout
                and "state = running" in process.stdout
                and "pid =" in process.stdout):
            try:
                status = _json(state / "status.json")
                stamp = datetime.fromisoformat(status["updated_at"])
                age = (datetime.now(timezone.utc) - stamp).total_seconds()
                healthy = (
                    stamp.tzinfo is not None
                    and 0 <= age < 30
                    and status.get("self_revision") == BASE_SHA
                    and status.get("state") in ("running", "idle")
                )
            except (OSError, ValueError, TypeError, KeyError):
                healthy = False
        if healthy:
            if stable_at is None:
                stable_at = time.monotonic()
            elif time.monotonic() - stable_at >= 2:
                return
        else:
            stable_at = None
        time.sleep(0.5)
    raise CutoverBlocked("B1c launchd registered but no stable live supervisor heartbeat")


def _read_plist(path: Path, digest: str) -> dict:
    if path.is_symlink() or not path.is_file():
        raise CutoverBlocked("launchd plist unavailable")
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != digest:
        raise CutoverBlocked("launchd plist SHA mismatch")
    try:
        value = plistlib.loads(raw)
    except (ValueError, TypeError) as exc:
        raise CutoverBlocked("invalid launchd plist") from exc
    if not isinstance(value, dict) or value.get("Label") != LABEL:
        raise CutoverBlocked("wrong launchd service label")
    return value


def prepare(home: Path) -> dict:
    """Prepare only immutable rollback, new plist and external controller job."""
    root, state, candidate, plist = _validate_paths(home)
    if (root / JOURNAL).exists():
        raise CutoverBlocked("existing cutover journal; preserve it")
    manifest = _json(root / "staging-manifest.json")
    baseline_file = root / "configuration-backup/com.michal.local-agent.plist"
    raw = baseline_file.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    if digest != manifest["baseline_plist_sha256"] or plist.read_bytes() != raw:
        raise CutoverBlocked("live B1c plist no longer equals backup")
    document = _read_plist(baseline_file, digest)
    argv = document.get("ProgramArguments")
    if (not isinstance(argv, list) or len(argv) != 6
            or argv[1] != manifest["baseline_source_path"] + "/agent_entrypoint.py"
            or document.get("WorkingDirectory") != manifest["baseline_source_path"]
            or document.get("EnvironmentVariables", {}).get("LOCAL_AGENT_MACOS_ADMISSION_REQUIRED") != "1"):
        raise CutoverBlocked("B1c launchd policy mismatch")
    replacement = dict(document)
    new_argv = list(argv)
    new_argv[1] = str(candidate / "agent_entrypoint.py")
    replacement["ProgramArguments"] = new_argv
    replacement["WorkingDirectory"] = str(candidate)
    candidate_plist = plistlib.dumps(replacement, fmt=plistlib.FMT_XML, sort_keys=True)
    _atomic(root / "mvp-candidate.plist", candidate_plist)
    _atomic(root / "mvp-baseline.plist", raw)
    service_config = state / "supervisor-control.json"
    if service_config.exists() or service_config.is_symlink():
        raise CutoverBlocked("host intake already configured, refusing overwrite")
    control_checkout = root / "host-control"
    if control_checkout.is_symlink() or not (control_checkout / ".git").is_dir():
        raise CutoverBlocked("dedicated host Git control checkout not provisioned")
    origin = _git(control_checkout, "remote", "get-url", "origin")
    allowed = ("https://github.com/example-owner/local-agent", "https://github.com/example-owner/local-agent.git",
               "git@github.com:example-owner/local-agent", "git@github.com:example-owner/local-agent.git")
    if origin not in allowed or _git(control_checkout, "branch", "--show-current") != "agent-control":
        raise CutoverBlocked("host control checkout branch/origin mismatch")
    for key in ("user.name", "user.email"):
        if not _git(control_checkout, "config", "--local", "--get", key):
            raise CutoverBlocked("host control local Git identity missing")
    selected = {"version": 1, "enabled": True, "host_id": HOST_ID,
                "identity_file": str(state / "host-identity.json"),
                "control_checkout": str(control_checkout),
                "repository": "example-owner/local-agent",
                "host_intake": {"enabled": True, "authorization": "github_writers"}}
    _atomic(root / "mvp-host-config.json", (json.dumps(selected, sort_keys=True) + "\n").encode())
    controller = root / "mvp-controller.py"
    current = Path(__file__)
    if current.is_symlink():
        raise CutoverBlocked("controller source symlink")
    _atomic(controller, current.read_bytes())
    journal = {"version": 1, "state": "prepared",
               "candidate_sha": CANDIDATE_SHA, "baseline_sha": BASE_SHA,
               "plist_baseline_sha256": digest,
               "plist_candidate_sha256": hashlib.sha256(candidate_plist).hexdigest(),
               "controller_sha256": hashlib.sha256(controller.read_bytes()).hexdigest(),
               "prepared_at": datetime.now(timezone.utc).isoformat()}
    _save_state(root, journal, "prepared")
    job = {"Label": CONTROLLER_LABEL,
           "ProgramArguments": [sys.executable, "-B", str(controller), "watch",
                                "--home", str(home)],
           "WorkingDirectory": str(root), "RunAtLoad": True,
           "KeepAlive": False, "StartInterval": 20,
           "EnvironmentVariables": {"PYTHONDONTWRITEBYTECODE": "1"},
           "StandardOutPath": str(root / "mvp-watch.out"),
           "StandardErrorPath": str(root / "mvp-watch.err")}
    # Prepare is inert: only an explicit later launchctl bootstrap arms cutover.
    jobpath = root / "mvp-watch.plist"
    if jobpath.exists() or jobpath.is_symlink():
        raise CutoverBlocked("existing independent release job")
    _atomic(jobpath, plistlib.dumps(job, fmt=plistlib.FMT_XML))
    return {"state": "prepared", "jobpath": str(jobpath), "candidate_sha": CANDIDATE_SHA,
            "production_service_changed": False, "external_controller": str(controller)}


def _queue_idle(state: Path) -> bool:
    status = _json(state / "status.json")
    if status.get("state") not in ("running", "idle") or status.get("self_revision") != BASE_SHA:
        return False
    stamp = datetime.fromisoformat(status["updated_at"])
    if stamp.tzinfo is None or abs((datetime.now(timezone.utc) - stamp).total_seconds()) > 100:
        return False
    registry = _json(state / "repositories.json")
    ids = [r.get("id") for r in registry.get("repositories", [])]
    if len(ids) != 9 or len(set(ids)) != 9 or "local-agent" not in ids:
        raise CutoverBlocked("unexpected registry at release drain")
    for ident in ids:
        for leaf in ("claims", "result-spool", "corrupt-claims"):
            path = state / "repositories" / ident / leaf
            if not path.is_dir() and leaf != "corrupt-claims":
                raise CutoverBlocked("missing release ledger")
            if path.is_dir() and any(p.name != ".gitkeep" for p in path.iterdir()):
                return False
    for leaf in ("claims", "result-spool"):
        path = state / leaf
        if not path.is_dir() or any(p.name != ".gitkeep" for p in path.iterdir()):
            return False
    return True


def _frozen_path(state: Path) -> Path:
    return state / FROZEN


def _freeze(state: Path) -> int:
    lock = state / LOCK
    if lock.is_symlink() or not lock.is_file():
        raise CutoverBlocked("live release admission lock absent")
    fd = os.open(lock, os.O_RDWR | getattr(os, "O_NOFOLLOW", 0))
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if _frozen_path(state).exists():
            raise CutoverBlocked("another release freeze is active")
        return fd
    except BaseException:
        os.close(fd)
        raise


def _freeze_accepted(state: Path) -> int:
    """Finish an accepted-but-frozen service transition without task replay."""
    lock = state / LOCK
    if lock.is_symlink() or not lock.is_file():
        raise CutoverBlocked("required release admission lock missing")
    fd = os.open(lock, os.O_RDWR | getattr(os, "O_NOFOLLOW", 0))
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        marker = _frozen_path(state)
        if marker.is_symlink() or not marker.is_file() or marker.read_bytes() != b'{"state":"frozen"}\n':
            raise CutoverBlocked("untrusted accepted-release freeze marker")
        return fd
    except BaseException:
        os.close(fd)
        raise


def _restore(root: Path, state: Path, plist: Path, record: dict) -> dict:
    """Independent recovery: never replay commands or delete task ledgers."""
    baseline = _json(root / "staging-manifest.json")["baseline_source_path"] + "/agent_entrypoint.py"
    candidate = str(root / "candidate-7df83dcd" / "agent_entrypoint.py")
    target = f"gui/{os.getuid()}/{LABEL}"
    domain = f"gui/{os.getuid()}"
    expected = record["plist_baseline_sha256"]
    _read_plist(root / "mvp-baseline.plist", expected)
    backup = root / "baseline-B1c"
    if (_git(backup, "rev-parse", "HEAD") != BASE_SHA
            or _git(backup, "status", "--porcelain")):
        raise CutoverBlocked("trusted independent B1c backup changed")
    # Validate *all* rollback proofs before the first local mutation.
    config = state / "supervisor-control.json"
    if config.is_symlink():
        raise CutoverBlocked("host intake config symlink; leave frozen")
    if config.exists() and config.read_bytes() != (root / "mvp-host-config.json").read_bytes():
        raise CutoverBlocked("host intake config unexpectedly changed; leave frozen")
    current_hash = hashlib.sha256(plist.read_bytes()).hexdigest()
    if current_hash not in (record["plist_baseline_sha256"], record["plist_candidate_sha256"]):
        raise CutoverBlocked("installed plist modified outside release; leave frozen")
    marker = _frozen_path(state)
    if not marker.is_file() or marker.read_bytes() != b'{"state":"frozen"}\n':
        raise CutoverBlocked("trusted release freeze missing; leave frozen")
    # Keep admission frozen until the original B1c service is registered.
    if config.exists():
        config.unlink()
    _atomic(plist, (root / "mvp-baseline.plist").read_bytes())
    generation = _generation(target, baseline, candidate)
    if generation == "candidate":
        _bootout_and_wait(target, baseline, candidate)
        generation = "absent"
    if generation == "absent":
        _bootstrap_with_retry(
            domain, plist, target, expected="baseline",
            baseline=baseline, candidate=candidate,
        )
    if _generation(target, baseline, candidate) != "baseline":
        raise CutoverBlocked("independent B1c rollback failed; intake remains frozen")
    _wait_healthy_baseline(state, target, baseline, candidate)
    # Persist completion while the intake is still frozen. On an interrupted
    # thaw the next watchdog can verify live B1c and remove only this marker.
    record = _save_state(root, record, "rolled_back")
    marker.unlink()
    return record


def watch(home: Path, *, health_timeout_seconds: int = 95) -> dict:
    if type(health_timeout_seconds) is not int or not 2 <= health_timeout_seconds <= 95:
        raise CutoverBlocked("invalid health acceptance timeout")
    root, state, candidate_dir, plist = _validate_paths(home, require_candidate=False)
    record = _load_state(root)
    if record.get("candidate_sha") != CANDIDATE_SHA or record.get("baseline_sha") != BASE_SHA:
        raise CutoverBlocked("release transaction source mismatch")
    script = root / "mvp-controller.py"
    if hashlib.sha256(script.read_bytes()).hexdigest() != record["controller_sha256"]:
        raise CutoverBlocked("independent recovery source changed")
    if record["state"] == "rolled_back":
        baseline_source = _json(root / "staging-manifest.json")["baseline_source_path"]
        if _generation(
            f"gui/{os.getuid()}/{LABEL}", baseline_source + "/agent_entrypoint.py",
            str(candidate_dir / "agent_entrypoint.py"),
        ) != "baseline":
            raise CutoverBlocked("rollback journal exists but B1c launchd is not registered")
        marker = _frozen_path(state)
        if marker.exists() or marker.is_symlink():
            fd = _freeze_accepted(state)
            try:
                marker.unlink()
            finally:
                os.close(fd)
        return {"state": "rolled_back", "idempotent": True}
    if record["state"] == "accepted":
        marker = _frozen_path(state)
        if not marker.exists():
            return {"state": "accepted", "idempotent": True}
        # Journal is accepted while admission is still frozen. After a
        # crash in that window, thaw only the verified idle candidate.
        fd = _freeze_accepted(state)
        try:
            original = _json(root / "staging-manifest.json")["baseline_source_path"]
            if (_generation(f"gui/{os.getuid()}/{LABEL}",
                            original + "/agent_entrypoint.py",
                            str(candidate_dir / "agent_entrypoint.py")) != "candidate"):
                raise CutoverBlocked("accepted release service changed; remain frozen")
            status = _json(state / "status.json")
            stamp = datetime.fromisoformat(status["updated_at"])
            if (status.get("self_revision") != CANDIDATE_SHA
                    or status.get("state") not in ("running", "idle")
                    or stamp.tzinfo is None
                    or abs((datetime.now(timezone.utc) - stamp).total_seconds()) > 45):
                raise CutoverBlocked("accepted release has no current candidate status")
            marker.unlink()
            return {"state": "accepted", "recovered_frozen_acceptance": True}
        finally:
            os.close(fd)
    baseline = _json(root / "staging-manifest.json")["baseline_source_path"] + "/agent_entrypoint.py"
    candidate = str(candidate_dir / "agent_entrypoint.py")
    target = f"gui/{os.getuid()}/{LABEL}"
    domain = f"gui/{os.getuid()}"
    if record["state"] == "switching":
        # A previously killed watchdog must never retry the candidate.
        _restore(root, state, plist, record)
        return {"state": "rolled_back", "reason": "interrupted_candidate"}
    if record["state"] != "prepared":
        raise CutoverBlocked("unknown release journal state")
    # A damaged candidate must not prevent interrupted-switch rollback.
    if (_git(candidate_dir, "rev-parse", "HEAD") != CANDIDATE_SHA
            or _git(candidate_dir, "status", "--porcelain")):
        raise CutoverBlocked("candidate drift before cutover")
    if not _queue_idle(state):
        return {"state": "prepared", "waiting_for_safe_drain": True}
    try:
        fd = _freeze(state)
    except BlockingIOError:
        # An existing project task legitimately holds a shared admission
        # lease. Do not throw a traceback or try a competing cutover.
        return {"state": "prepared", "waiting_for_active_task_lease": True}
    try:
        if not _queue_idle(state):
            return {"state": "prepared", "waiting_for_safe_drain": True}
        if _generation(target, baseline, candidate) != "baseline":
            raise CutoverBlocked("running source not B1c")
        if plist.read_bytes() != (root / "mvp-baseline.plist").read_bytes():
            raise CutoverBlocked("live B1c launchd plist drift")
        # Durable journal and freeze before the first switch effect.
        _atomic(_frozen_path(state), b'{"state":"frozen"}\n')
        record = _save_state(root, record, "switching")
        try:
            _atomic(state / "supervisor-control.json", (root / "mvp-host-config.json").read_bytes())
            _atomic(plist, (root / "mvp-candidate.plist").read_bytes())
            _bootout_and_wait(target, baseline, candidate)
            _bootstrap_with_retry(
                domain, plist, target, expected="candidate",
                baseline=baseline, candidate=candidate,
            )
            if _generation(target, baseline, candidate) != "candidate":
                raise CutoverBlocked("candidate launchd generation absent")
            deadline = time.monotonic() + health_timeout_seconds
            while time.monotonic() < deadline:
                try:
                    status = _json(state / "status.json")
                    fresh = datetime.fromisoformat(status["updated_at"])
                    if (fresh.tzinfo is not None and
                            (datetime.now(timezone.utc) - fresh).total_seconds() < 20 and
                            status.get("self_revision") == CANDIDATE_SHA and
                            status.get("state") in ("running", "idle")):
                        _save_state(root, record, "accepted")
                        _frozen_path(state).unlink()
                        return {"state": "accepted", "candidate_sha": CANDIDATE_SHA}
                except (ValueError, OSError, KeyError):
                    pass
                time.sleep(1)
            raise CutoverBlocked("candidate heartbeat not accepted")
        except BaseException:
            _restore(root, state, plist, record)
            raise
    finally:
        os.close(fd)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Externally recovered Mac MVP cutover")
    parser.add_argument("action", choices=("prepare", "watch"))
    parser.add_argument("--home", type=Path, required=True)
    opts = parser.parse_args(argv)
    result = prepare(opts.home) if opts.action == "prepare" else watch(opts.home)
    if not result.get("idempotent"):
        print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
