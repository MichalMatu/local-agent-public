#!/usr/bin/env python3
"""Interactive macOS repository maintenance with independent crash rollback.

Run from the accepted installed source in a normal Terminal (not as an
agent task). The menu is the existing CLI; this wrapper only owns one
guarded offline service pause and externally recovered restart.
"""
from __future__ import annotations

import fcntl
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import plistlib
import secrets
import subprocess
import sys
import time
from datetime import datetime, timezone

LABEL = "com.michal.local-agent"
WATCH_LABEL = "dev.localagent.repository-maintenance"
FROZEN = "macos-release-admission-frozen.json"
WATCH_SUFFIX = ".plist"
JOURNAL = "maintenance-journal.json"


class MaintenanceBlocked(RuntimeError):
    pass


def _atomic(path: Path, data: bytes) -> None:
    if path.is_symlink():
        raise MaintenanceBlocked("refuse symlinked maintenance target")
    temporary = path.parent / (".maintenance-" + secrets.token_hex(12))
    fd = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "wb", closefd=False) as out:
            out.write(data)
            out.flush()
            os.fsync(out.fileno())
        os.replace(temporary, path)
        fdparent = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(fdparent)
        finally:
            os.close(fdparent)
    finally:
        os.close(fd)
        temporary.unlink(missing_ok=True)


def _json(path: Path) -> dict:
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 4_000_000:
        raise MaintenanceBlocked(f"invalid maintenance record: {path.name}")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise MaintenanceBlocked(f"invalid maintenance object: {path.name}")
    return value


def _write_journal(root: Path, record: dict, status: str) -> dict:
    updated = {**record, "state": status}
    _atomic(root / JOURNAL, (json.dumps(updated, sort_keys=True) + "\n").encode())
    return updated


def _launch(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(("/bin/launchctl", *args), capture_output=True,
                          text=True, timeout=30)


def _target() -> str:
    return f"gui/{os.getuid()}/{LABEL}"


def _watch_target() -> str:
    return f"gui/{os.getuid()}/{WATCH_LABEL}"


def _generation(source: Path) -> str:
    p = _launch("print", _target())
    if p.returncode != 0:
        return "absent"
    if str(source / "agent_entrypoint.py") not in p.stdout:
        raise MaintenanceBlocked("unexpected live launchd generation")
    # launchctl bootout can return before its job disappears from
    # 'launchctl print'. Preserve identity validation and allow the
    # same verified job to finish draining in its registered-but-not-
    # running state; never infer that it has already gone away.
    return "running" if "state = running" in p.stdout else "stopping"


def _wait_absent(source: Path) -> None:
    deadline = time.monotonic() + 35
    while time.monotonic() < deadline:
        if _generation(source) == "absent":
            return
        time.sleep(0.5)
    raise MaintenanceBlocked("service bootout never completed")


def _resume(source: Path, plist: Path, sha: str, state: Path) -> None:
    target = _generation(source)
    if target == "stopping":
        _wait_absent(source)
        target = "absent"
    bootstrapped_at = None
    if target == "absent":
        bootstrapped_at = datetime.now(timezone.utc)
        launched = _launch("bootstrap", f"gui/{os.getuid()}", str(plist))
        if launched.returncode != 0 and _generation(source) != "running":
            raise MaintenanceBlocked("launchd service restart failed")
    deadline = time.monotonic() + 85
    while time.monotonic() < deadline:
        try:
            s = _json(state / "status.json")
            stamp = datetime.fromisoformat(s["updated_at"])
            age = (datetime.now(timezone.utc) - stamp).total_seconds()
            if (_generation(source) == "running" and
                    s.get("self_revision") == sha and
                    s.get("state") in ("running", "idle") and
                    stamp.tzinfo is not None and 0 <= age < 25 and
                    (bootstrapped_at is None or stamp > bootstrapped_at)):
                return
        except (ValueError, OSError, KeyError, MaintenanceBlocked):
            pass
        time.sleep(0.5)
    raise MaintenanceBlocked("no healthy exact-source heartbeat after restart")


def _lock(path: Path, *, blocking: bool = False) -> int:
    if path.is_symlink() or not path.is_file():
        raise MaintenanceBlocked(f"maintenance lock unavailable: {path.name}")
    fd = os.open(path, os.O_RDWR | os.O_NOFOLLOW)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        return fd
    except BaseException:
        os.close(fd)
        raise


def _release(fd: int) -> None:
    fcntl.flock(fd, fcntl.LOCK_UN)
    os.close(fd)


def _state_paths(home: Path) -> tuple[Path, Path]:
    if sys.platform != "darwin" or os.getuid() == 0 or os.getuid() != os.geteuid():
        raise MaintenanceBlocked("normal non-root macOS user required")
    state = home / "Library/Application Support/local-agent"
    plist = home / "Library/LaunchAgents" / (LABEL + WATCH_SUFFIX)
    if state.is_symlink() or not state.is_dir() or plist.is_symlink() or not plist.is_file():
        raise MaintenanceBlocked("untrusted installed state or service plist")
    return state, plist


def _identify_running_source(home: Path, state: Path, plist: Path) -> tuple[Path, str]:
    p = plistlib.loads(plist.read_bytes())
    args = p.get("ProgramArguments")
    path = Path(p.get("WorkingDirectory", ""))
    if (not isinstance(args, list) or len(args) < 2
            or not path.is_absolute() or path.is_symlink()
            or args[1] != str(path / "agent_entrypoint.py")
            or p.get("Label") != LABEL):
        raise MaintenanceBlocked("installed launchd source/label mismatch")
    git = subprocess.run(("git", "-C", str(path), "rev-parse", "HEAD"),
                         capture_output=True, text=True, timeout=15)
    dirty = subprocess.run(("git", "-C", str(path), "status", "--porcelain"),
                           capture_output=True, text=True, timeout=15)
    if git.returncode or dirty.returncode or dirty.stdout.strip():
        raise MaintenanceBlocked("installed source not a clean pinned Git checkout")
    revision = git.stdout.strip()
    status = _json(state / "status.json")
    if status.get("self_revision") != revision or _generation(path) != "running":
        raise MaintenanceBlocked("launchd source does not match live heartbeat")
    if (state / FROZEN).exists() or (state / FROZEN).is_symlink():
        raise MaintenanceBlocked("another release/maintenance admission freeze is active")
    return path, revision


def _quiescent(state: Path, source: Path, revision: str) -> None:
    """Read-only lease + queue guard, before/after acquiring admission."""
    # Import only from a verified exact-source checkout.
    sys.path.insert(0, str(source))
    from local_agent.repository.context import load_repository_registry
    from local_agent.repository.registration_disable import _host_claims_have_terminal_results
    from local_agent.repository.registry_selection import _check_project_ledgers
    from local_agent.foundation.lease_recovery import repository_lease_paths, repository_leases_busy
    registry = load_repository_registry(home=Path.home(),
                                        path=state / "repositories.json",
                                        include_disabled=True, allow_empty=True)
    _check_project_ledgers(state, (x.repository_id for x in registry))
    for leaf in ("claims", "result-spool", "corrupt-claims"):
        p = state / leaf
        if p.is_symlink() or not p.is_dir() or any(x.name != ".gitkeep" for x in p.iterdir()):
            raise MaintenanceBlocked(f"global queue not empty: {leaf}")
    _host_claims_have_terminal_results(state)
    if repository_leases_busy(repository_lease_paths(registry, state_dir=state)):
        raise MaintenanceBlocked("repository execution lease is still busy")
    s = _json(state / "status.json")
    stamp = datetime.fromisoformat(s["updated_at"])
    age = (datetime.now(timezone.utc) - stamp).total_seconds()
    if (stamp.tzinfo is None or not 0 <= age <= 35 or
            s.get("self_revision") != revision or
            s.get("state") not in ("running", "idle") or
            s.get("current_task_id") is not None):
        raise MaintenanceBlocked("supervisor is running work or heartbeat stale")


def _owned_freeze(record: dict) -> bytes:
    """Tie the frozen admission to the exact independent session journal."""
    ident = record.get("session_id")
    if (not isinstance(ident, str) or len(ident) != 32 or
            not ident.startswith("session-") or
            any(c not in "0123456789abcdef" for c in ident[8:])):
        raise MaintenanceBlocked("invalid maintenance session identity")
    return (json.dumps({"owner": ident, "state": "frozen", "version": 1},
                       sort_keys=True) + "\n").encode()


def _freeze_drain(state: Path, record: dict) -> None:
    """Write-once admission barrier BEFORE waiting for active turns to finish.

    Existing workers that already have their shared admission lease may
    finish and publish their result. Fresh admission checks the marker and
    refuses to start another turn. A competing maintainer cannot overwrite
    an existing freeze; release controllers also refuse this marker.
    """
    marker = state / FROZEN
    fd = os.open(marker, os.O_CREAT | os.O_EXCL | os.O_WRONLY |
                 getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        with os.fdopen(fd, "wb", closefd=False) as stream:
            stream.write(_owned_freeze(record))
            stream.flush()
            os.fsync(stream.fileno())
    finally:
        os.close(fd)
    parent = os.open(state, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(parent)
    finally:
        os.close(parent)


def _await_drain(state: Path, source: Path, revision: str, record: dict,
                 *, timeout_seconds: float = 240) -> int:
    """Acquire exclusive admission and verify completed ledgers; never kill work."""
    # The standalone Terminal controller starts from scripts/, not from a
    # Python package checkout. Import only the verified installed source.
    sys.path.insert(0, str(source))
    from local_agent.supervisor.macos_release_admission import _active_config
    if not _active_config(state):
        raise MaintenanceBlocked("installed Mac task admission fence is not enabled")
    deadline = time.monotonic() + timeout_seconds
    admission = None
    last_reason = "waiting for task admission"
    while time.monotonic() < deadline:
        marker = state / FROZEN
        if (marker.is_symlink() or not marker.is_file() or
                marker.read_bytes() != _owned_freeze(record)):
            raise MaintenanceBlocked("maintenance admission barrier changed")
        if _generation(source) != "running":
            raise MaintenanceBlocked("source changed while waiting for task drain")
        if admission is None:
            try:
                admission = _lock(state / "macos-release-admission.lock")
            except BlockingIOError:
                last_reason = "existing tasks still hold admission"
        if admission is not None:
            try:
                _quiescent(state, source, revision)
            except (MaintenanceBlocked, RuntimeError) as exc:
                last_reason = str(exc)
            else:
                return admission
        time.sleep(1)
    if admission is not None:
        _release(admission)
    raise MaintenanceBlocked("safe task drain timed out: " + last_reason)


def _thaw_before_shutdown(root: Path, record: dict,
                          source: Path, state: Path) -> None:
    """Restore admission without bootout if service has NOT been stopped."""
    if _generation(source) != "running":
        raise MaintenanceBlocked("do not thaw an unverified live service")
    registry = state / "repositories.json"
    original = (root / "registry-backup.json").read_bytes()
    if registry.is_symlink() or registry.read_bytes() != original:
        raise MaintenanceBlocked("pre-shutdown registry changed unexpectedly")
    marker = state / FROZEN
    if marker.is_symlink():
        raise MaintenanceBlocked("untrusted pre-shutdown admission marker")
    if marker.exists() and marker.read_bytes() != _owned_freeze(record):
        raise MaintenanceBlocked("foreign pre-shutdown admission marker")
    _write_journal(root, record, "rolled_back")
    if marker.exists():
        marker.unlink()


def _control_move_plan(state: Path, source: Path) -> dict | None:
    """Choose one stable transport location; never clone or repair the inbox."""
    sys.path.insert(0, str(source))
    from local_agent.supervisor.host_control import load_host_control_source, validate_host_control_checkout
    selected = load_host_control_source(state / "supervisor-control.json")
    if selected is None:
        raise MaintenanceBlocked("enabled host transport required for relocation")
    validate_host_control_checkout(selected, production_checkout=source)
    destination = state / "transport/agent-control"
    if selected.checkout == destination:
        return None
    if selected.checkout in destination.parents or destination in selected.checkout.parents:
        raise MaintenanceBlocked("transport paths overlap")
    parent = destination.parent
    if parent.is_symlink() or (parent.exists() and not parent.is_dir()):
        raise MaintenanceBlocked("untrusted transport directory")
    parent.mkdir(mode=0o700, exist_ok=True)
    if parent.stat().st_uid != os.getuid() or parent.stat().st_mode & 0o077:
        raise MaintenanceBlocked("transport directory is not private")
    if destination.exists() or destination.is_symlink():
        raise MaintenanceBlocked("transport destination occupied; preserve both copies")
    if selected.checkout.stat().st_dev != parent.stat().st_dev:
        raise MaintenanceBlocked("transport relocation requires one filesystem")
    return {"old": str(selected.checkout), "new": str(destination),
            "config_sha256": hashlib.sha256((state / "supervisor-control.json").read_bytes()).hexdigest()}


def _move_control(root: Path, record: dict, source: Path, state: Path, *, rollback: bool = False) -> None:
    """Offline leased rename with idempotent recovery across either commit boundary."""
    sys.path.insert(0, str(source))
    from local_agent.foundation.process import acquire_execution_leases, fsync_directory
    from local_agent.repository.registration_disable import _require_offline_installation
    from local_agent.supervisor.host_control import (
        host_control_lease_keys, load_host_control_source, validate_host_control_checkout,
    )
    backup = root / "host-control-backup.json"
    if backup.is_symlink():
        raise MaintenanceBlocked("untrusted host control backup")
    saved = backup.read_bytes()
    if hashlib.sha256(saved).hexdigest() != record["host_control_sha256"]:
        raise MaintenanceBlocked("host control backup changed")
    original = json.loads(saved)
    old, new = Path(record["control_move"]["old"]), Path(record["control_move"]["new"])
    if old != Path(original["control_checkout"]) or new != state / "transport/agent-control":
        raise MaintenanceBlocked("host transport move identity changed")
    updated = (json.dumps({**original, "control_checkout": str(new)}, sort_keys=True) + "\n").encode()
    config = state / "supervisor-control.json"
    if config.is_symlink() or config.read_bytes() not in (saved, updated):
        raise MaintenanceBlocked("host configuration changed outside maintenance")
    selected = load_host_control_source(root / "host-control-backup.json")
    if selected is None or new.parent.is_symlink():
        raise MaintenanceBlocked("untrusted host relocation state")
    target = replace(selected, checkout=new)
    keys = (*host_control_lease_keys(selected), *host_control_lease_keys(target))
    with _require_offline_installation(state):
        leases = acquire_execution_leases(state / "locks/repositories", keys)
        try:
            if old.is_symlink() or new.is_symlink() or old.exists() == new.exists():
                raise MaintenanceBlocked("ambiguous transport locations; preserve both")
            current = old if old.exists() else new
            validate_host_control_checkout(replace(selected, checkout=current), production_checkout=source)
            if not (current / ".git").is_dir() or (current / ".git/objects/info/alternates").exists():
                raise MaintenanceBlocked("transport must be an independent Git clone")
            configured = subprocess.run(("git", "-C", str(current), "config", "--get", "core.worktree"),
                                        capture_output=True, text=True, timeout=15)
            if configured.returncode != 1:
                raise MaintenanceBlocked("transport has explicit or invalid Git worktree configuration")
            for args in (("status", "--porcelain"), ("rev-parse", "--git-common-dir")):
                git = subprocess.run(("git", "-C", str(current), *args),
                                     capture_output=True, text=True, timeout=15, check=True)
                expected = "" if args[0] == "status" else ".git"
                if git.stdout.strip() != expected:
                    raise MaintenanceBlocked("transport has local work or shared Git state")
            desired = old if rollback else new
            if current != desired:
                os.rename(current, desired)
                fsync_directory(current.parent)
                fsync_directory(desired.parent)
            _atomic(config, saved if rollback else updated)
        finally:
            leases.close()


def _new_session(home: Path, state: Path, plist: Path, source: Path, sha: str,
                 *, control_move: dict | None = None) -> tuple[Path, int]:
    base = home / "Library/Application Support/local-agent-maintenance"
    if base.is_symlink():
        raise MaintenanceBlocked("maintenance root symlink")
    base.mkdir(mode=0o700, exist_ok=True)
    if base.stat().st_uid != os.getuid() or base.stat().st_mode & 0o077:
        raise MaintenanceBlocked("maintenance parent is not private")
    agents = home / "Library/LaunchAgents"
    if (agents / (WATCH_LABEL + WATCH_SUFFIX)).exists():
        raise MaintenanceBlocked("another repository maintenance watch exists")
    if any(agents.glob("dev.localagent.cli-rollout.*.plist")):
        raise MaintenanceBlocked("an independent release watchdog is still armed")
    root = base / ("session-" + secrets.token_hex(12))
    root.mkdir(mode=0o700)
    lock = root / "session.lock"
    lock.touch(mode=0o600, exist_ok=False)
    fd = _lock(lock)
    try:
        original = (state / "repositories.json").read_bytes()
        if (state / "repositories.json").is_symlink():
            raise MaintenanceBlocked("registry path is linked")
        _atomic(root / "registry-backup.json", original)
        _atomic(root / "service.plist", plist.read_bytes())
        _atomic(root / "controller.py", Path(__file__).read_bytes())
        record = {
            "version": 1, "source_sha": sha, "source_path": str(source),
            "registry_sha256": hashlib.sha256(original).hexdigest(),
            "plist_sha256": hashlib.sha256(plist.read_bytes()).hexdigest(),
            "controller_sha256": hashlib.sha256((root / "controller.py").read_bytes()).hexdigest(),
            "session_id": root.name,
        }
        if control_move is not None:
            raw = (state / "supervisor-control.json").read_bytes()
            if hashlib.sha256(raw).hexdigest() != control_move["config_sha256"]:
                raise MaintenanceBlocked("host configuration changed since relocation preflight")
            _atomic(root / "host-control-backup.json", raw)
            record.update(control_move=control_move, host_control_sha256=hashlib.sha256(raw).hexdigest())
        _write_journal(root, record, "prepared")
        job = {
            "Label": WATCH_LABEL,
            "ProgramArguments": [sys.executable, "-B", str(root / "controller.py"),
                                 "watch", str(root)],
            "WorkingDirectory": str(root), "RunAtLoad": True,
            "KeepAlive": False, "StartInterval": 15,
            "StandardOutPath": str(root / "watch.out"),
            "StandardErrorPath": str(root / "watch.err"),
        }
        _atomic(root / "watch.plist", plistlib.dumps(job))
        watchpath = home / "Library/LaunchAgents" / (WATCH_LABEL + WATCH_SUFFIX)
        flags = os.O_EXCL | os.O_CREAT | os.O_WRONLY | os.O_NOFOLLOW
        outfile = os.open(watchpath, flags, 0o600)
        try:
            with os.fdopen(outfile, "wb", closefd=False) as out:
                out.write((root / "watch.plist").read_bytes())
                out.flush()
                os.fsync(out.fileno())
        finally:
            os.close(outfile)
        armed = _launch("bootstrap", f"gui/{os.getuid()}", str(watchpath))
        if armed.returncode != 0:
            if watchpath.read_bytes() == (root / "watch.plist").read_bytes():
                watchpath.unlink()
            raise MaintenanceBlocked("independent recovery watchdog bootstrap failed")
        return root, fd
    except BaseException:
        _release(fd)
        raise


def _verify_session(root: Path) -> tuple[dict, Path, Path, Path, Path]:
    if root.is_symlink() or not root.is_dir():
        raise MaintenanceBlocked("untrusted session directory")
    record = _json(root / JOURNAL)
    source = Path(record["source_path"])
    home = Path.home()
    state, plist = _state_paths(home)
    if hashlib.sha256((root / "controller.py").read_bytes()).hexdigest() != record["controller_sha256"]:
        raise MaintenanceBlocked("independent controller changed")
    if hashlib.sha256((root / "registry-backup.json").read_bytes()).hexdigest() != record["registry_sha256"]:
        raise MaintenanceBlocked("independent registry backup corrupted")
    if (hashlib.sha256((root / "service.plist").read_bytes()).hexdigest() !=
            record["plist_sha256"] or plist.read_bytes() != (root / "service.plist").read_bytes()):
        raise MaintenanceBlocked("production launchd plist changed")
    git = subprocess.run(("git", "-C", str(source), "rev-parse", "HEAD"),
                         capture_output=True, text=True, timeout=15)
    if git.returncode or git.stdout.strip() != record["source_sha"]:
        raise MaintenanceBlocked("accepted source changed")
    if record.get("control_move") is not None:
        backup = root / "host-control-backup.json"
        if backup.is_symlink() or hashlib.sha256(backup.read_bytes()).hexdigest() != record["host_control_sha256"]:
            raise MaintenanceBlocked("independent host control backup corrupted")
    return record, source, state, plist, home


def _rollback(root: Path, record: dict, source: Path, state: Path, plist: Path) -> None:
    """Fails closed: only revive the verified existing service generation."""
    current_generation = _generation(source)
    if current_generation == "running":
        stop = _launch("bootout", _target())
        if stop.returncode:
            raise MaintenanceBlocked("cannot stop service for independent rollback")
        _wait_absent(source)
    elif current_generation == "stopping":
        # Owner bootout may already be in flight when the first
        # synchronous rollback or independent watcher takes over.
        _wait_absent(source)
    saved = (root / "registry-backup.json").read_bytes()
    registry = state / "repositories.json"
    if registry.is_symlink() or not registry.is_file():
        raise MaintenanceBlocked("registry untrusted during restore")
    _atomic(registry, saved)
    if record.get("control_move") is not None:
        _move_control(root, record, source, state, rollback=True)
    _resume(source, plist, record["source_sha"], state)
    _write_journal(root, record, "rolled_back")
    marker = state / FROZEN
    if marker.is_symlink() or (marker.exists() and
                               marker.read_bytes() != _owned_freeze(record)):
        raise MaintenanceBlocked("rollback marker is foreign; preserve it")
    if marker.exists():
        marker.unlink()


def _finish(root: Path, record: dict, source: Path, state: Path, plist: Path) -> None:
    from local_agent.repository.context import load_repository_registry
    from local_agent.repository.registry_selection import _check_project_ledgers
    contexts = load_repository_registry(home=Path.home(),
                                        path=state / "repositories.json",
                                        include_disabled=True, allow_empty=True)
    _check_project_ledgers(state, (x.repository_id for x in contexts))
    _write_journal(root, record, "restarting")
    _resume(source, plist, record["source_sha"], state)
    _write_journal(root, record, "accepted")
    marker = state / FROZEN
    if marker.is_symlink() or marker.read_bytes() != _owned_freeze(record):
        raise MaintenanceBlocked("invalid trusted admission freeze")
    marker.unlink()


def _retire(root: Path, home: Path) -> None:
    target = home / "Library/LaunchAgents" / (WATCH_LABEL + WATCH_SUFFIX)
    expected = (root / "watch.plist").read_bytes()
    if not target.is_file() or target.is_symlink() or target.read_bytes() != expected:
        raise MaintenanceBlocked("watcher identity changed; do not retire")
    registered = _launch("print", _watch_target())
    if (registered.returncode != 0
            or str(root / "controller.py") not in registered.stdout):
        raise MaintenanceBlocked("registered watchdog is not this session's controller")
    # If the watcher retires itself, launchctl may kill it before it
    # executes another Python statement. Remove the exact owned plist
    # first, then bootout; restore it if bootout is rejected.
    target.unlink()
    gone = _launch("bootout", _watch_target())
    if gone.returncode:
        fd = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
        try:
            with os.fdopen(fd, "wb", closefd=False) as out:
                out.write(expected)
                out.flush()
                os.fsync(out.fileno())
        finally:
            os.close(fd)
        raise MaintenanceBlocked("watcher bootout failed; preserved for recovery")


def watch(root: Path) -> None:
    """Independent launchd process: no task execution, no worker replay."""
    record, source, state, plist, home = _verify_session(root)
    fd = None
    try:
        fd = _lock(root / "session.lock")
    except BlockingIOError:
        return  # Interactive owner still holds exclusive maintenance lease.
    try:
        marker = state / FROZEN
        if marker.is_symlink():
            raise MaintenanceBlocked("untrusted admission freeze in recovery")
        if record["state"] == "accepted":
            if not marker.exists():
                # Acceptance is not proof that the service survived an
                # intervening reboot. Reestablish the verified generation.
                _resume(source, plist, record["source_sha"], state)
            if marker.exists():
                # Crash after journaling acceptance but before thaw:
                # only thaw the exact healthy installed generation.
                if marker.read_bytes() != _owned_freeze(record):
                    raise MaintenanceBlocked("untrusted accepted freeze marker")
                try:
                    _resume(source, plist, record["source_sha"], state)
                except MaintenanceBlocked:
                    _rollback(root, record, source, state, plist)
                else:
                    marker.unlink()
            _retire(root, home)
            return
        if record["state"] == "rolled_back":
            if not marker.exists():
                _resume(source, plist, record["source_sha"], state)
            if marker.exists():
                if marker.read_bytes() != _owned_freeze(record):
                    raise MaintenanceBlocked("untrusted rollback freeze marker")
                _resume(source, plist, record["source_sha"], state)
                marker.unlink()
            _retire(root, home)
            return
        if record["state"] in ("prepared", "draining"):
            # No bootout is legal before 'switching'. Never terminate
            # ongoing project work just to recover an interrupted drain.
            _thaw_before_shutdown(root, record, source, state)
            _retire(root, home)
            return
        if record["state"] in ("switching", "offline", "restarting"):
            _rollback(root, record, source, state, plist)
            _retire(root, home)
            return
        raise MaintenanceBlocked("unknown maintenance journal state")
    finally:
        _release(fd)


def maintain(*, operation: str = "menu") -> None:
    """Use one guarded lifecycle for menu, no-op probe and transport relocation."""
    if operation not in ("menu", "probe", "relocate-control"):
        raise MaintenanceBlocked("unsupported maintenance operation")
    home = Path.home()
    state, plist = _state_paths(home)
    source, revision = _identify_running_source(home, state, plist)
    if (operation != "relocate-control" and
            Path(__file__).resolve() != (source / "scripts/macos_repository_maintenance.py").resolve()):
        raise MaintenanceBlocked("run the controller only from installed accepted source")
    move = _control_move_plan(state, source) if operation == "relocate-control" else None
    if operation == "relocate-control" and move is None:
        print("Host transport already uses the stable private location; no changes.")
        return
    root, lease = _new_session(home, state, plist, source, revision, control_move=move)
    record = _json(root / JOURNAL)
    admission = None
    try:
        # Durable 'draining' journal BEFORE the marker: a crash here must
        # only thaw intake, never stop a still-busy project worker.
        record = _write_journal(root, record, "draining")
        _freeze_drain(state, record)
        print("New admission paused; waiting for active workers to finish...", flush=True)
        admission = _await_drain(state, source, revision, record)
        # Exclusive admission is held through the complete service cycle.
        _write_journal(root, record, "switching")
        stopped = _launch("bootout", _target())
        if stopped.returncode:
            raise MaintenanceBlocked("service launchd bootout failed")
        _wait_absent(source)
        # Verify actual exclusive daemon lock after its owner exits.
        from local_agent.repository.registration_disable import _require_offline_installation
        with _require_offline_installation(state):
            pass
        _write_journal(root, record, "offline")
        if move is not None:
            _move_control(root, record, source, state)
        elif operation == "menu":
            print("Local Agent paused safely. Repository changes are now offline.")
            from local_agent.cli.diagnostics import main as diagnostics_main
            argv = sys.argv
            old_session = os.environ.get("LOCAL_AGENT_MAINTENANCE_SESSION")
            try:
                sys.argv = ["local_agent.cli.diagnostics", "menu"]
                os.environ["LOCAL_AGENT_MAINTENANCE_SESSION"] = "1"
                result = diagnostics_main()
                if result != 0:
                    raise MaintenanceBlocked("interactive menu returned a nonzero status")
            finally:
                sys.argv = argv
                if old_session is None:
                    os.environ.pop("LOCAL_AGENT_MAINTENANCE_SESSION", None)
                else:
                    os.environ["LOCAL_AGENT_MAINTENANCE_SESSION"] = old_session
        _finish(root, record, source, state, plist)
        print("Local Agent restarted with verified source; repository data preserved.")
    except BaseException:
        try:
            current = _json(root / JOURNAL)
            if current.get("state") in ("prepared", "draining"):
                # Before 'switching', not even an interrupted drain has
                # authority to stop a running worker.
                _thaw_before_shutdown(root, current, source, state)
            else:
                _rollback(root, current, source, state, plist)
        except BaseException:
            print(f"Recovery pending; independent watchdog remains armed: {root}", file=sys.stderr)
            raise
        raise
    finally:
        if admission is not None:
            _release(admission)
        _release(lease)
    _retire(root, home)


def main() -> int:
    if len(sys.argv) == 2 and sys.argv[1] in ("menu", "probe", "relocate-control"):
        maintain(operation=sys.argv[1])
    elif len(sys.argv) == 3 and sys.argv[1] == "watch":
        watch(Path(sys.argv[2]))
    else:
        raise SystemExit("usage: macos_repository_maintenance.py menu | probe | relocate-control | watch ROOT")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
