"""Independent, explicitly pinned macOS Host Control release preparation.

A daemon only queues immutable intents. This script runs outside its worker
and uses the existing launchd rollout watcher for drain, switch and rollback.
No effects occur on import or from status inspection.
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import plistlib
import re
import signal
import stat
import subprocess
import sys
import time

from scripts import macos_cli_polling_rollout as rollout

CANONICAL_SOURCE = "https://github.com/MichalMatu/local-agent-public.git"
_ID = re.compile(r"^[A-Za-z0-9._-]{1,120}$")
_SHA = re.compile(r"^[0-9a-f]{40}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
INTENTS = "host-self-update-intents"
COMPLETED = "host-self-update-completed"
SERVICE_POLL_SECONDS = 10.0
UPDATE_TURN_TIMEOUT_SECONDS = 1500.0
_defer_service_stop = False
_service_stop_pending = False


class HostReleaseBlocked(ValueError):
    """No existing release or task state may be bypassed."""


def _private_file(path: Path, *, max_bytes: int = 8192) -> bytes:
    if path.is_symlink():
        raise HostReleaseBlocked("release evidence is symlinked")
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        meta = os.fstat(descriptor)
        if (
            not stat.S_ISREG(meta.st_mode)
            or meta.st_uid != os.getuid()
            or meta.st_mode & 0o077
            or not 0 < meta.st_size <= max_bytes
        ):
            raise HostReleaseBlocked("release evidence owner/mode/size invalid")
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            return stream.read(max_bytes + 1)
    finally:
        os.close(descriptor)


def _installed_plist(path: Path) -> bytes:
    """LaunchAgents may be world-readable but never writable by peers."""
    if path.is_symlink():
        raise HostReleaseBlocked("installed launchd plist is symlinked")
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        meta = os.fstat(descriptor)
        if (
            not stat.S_ISREG(meta.st_mode)
            or meta.st_uid != os.getuid()
            or meta.st_mode & 0o022
            or not 0 < meta.st_size <= 65536
        ):
            raise HostReleaseBlocked("untrusted installed launchd plist")
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            return stream.read(65537)
    finally:
        os.close(descriptor)


def _data(path: Path) -> dict:
    try:
        value = json.loads(_private_file(path))
    except (OSError, ValueError, UnicodeError) as exc:
        raise HostReleaseBlocked("private release evidence unavailable") from exc
    if not isinstance(value, dict):
        raise HostReleaseBlocked("release evidence must be an object")
    return value


def _sha(value: object) -> str:
    if type(value) is not str or _SHA.fullmatch(value) is None:
        raise HostReleaseBlocked("an exact lowercase commit SHA is required")
    return value


def _git(checkout: Path, *args: str) -> str:
    result = subprocess.run(
        ["/usr/bin/git", "-C", str(checkout), *args],
        capture_output=True, text=True, timeout=45, check=False,
    )
    if result.returncode:
        raise HostReleaseBlocked("staged Git source verification failed")
    return result.stdout.strip()


def _check_directory(path: Path, *, private: bool = True) -> None:
    meta = path.lstat()
    forbidden = 0o077 if private else 0o022
    if (
        not stat.S_ISDIR(meta.st_mode)
        or meta.st_uid != os.getuid()
        or meta.st_mode & forbidden
    ):
        raise HostReleaseBlocked("release directory is not private")


def _atomic(path: Path, data: dict) -> None:
    raw = (json.dumps(data, sort_keys=True) + "\n").encode()
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())
    parent = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(parent)
    finally:
        os.close(parent)


def _context(home: Path, request_id: str) -> tuple[Path, dict, dict, Path, bytes]:
    if (
        sys.platform != "darwin"
        or os.geteuid() != os.getuid()
        or os.getuid() == 0
        or not home.is_absolute()
    ):
        raise HostReleaseBlocked("requires the real non-root macOS service user")
    if _ID.fullmatch(request_id) is None:
        raise HostReleaseBlocked("invalid host update request id")
    state = home / "Library/Application Support/local-agent"
    _check_directory(state, private=False)
    identity = _data(state / "host-identity.json")
    intent = _data(state / INTENTS / (request_id + ".json"))
    if (
        set(intent) != {
            "version", "request_id", "candidate_sha", "target_host_id",
            "target_installation_id", "status",
        }
        or intent["version"] != 1
        or intent["request_id"] != request_id
        or intent["status"] != "pending_external_controller"
        or intent["target_host_id"] != identity.get("host_id")
        or intent["target_installation_id"] != identity.get("installation_id")
    ):
        raise HostReleaseBlocked("host update intent identity/status mismatch")
    _sha(intent["candidate_sha"])
    plist = home / "Library/LaunchAgents/com.michal.local-agent.plist"
    original = _installed_plist(plist)
    try:
        configuration = plistlib.loads(original)
    except (ValueError, TypeError) as exc:
        raise HostReleaseBlocked("invalid installed launchd configuration") from exc
    if (
        configuration.get("Label") != "com.michal.local-agent"
        or not isinstance(configuration.get("WorkingDirectory"), str)
    ):
        raise HostReleaseBlocked("unexpected installed service identity")
    source = Path(configuration["WorkingDirectory"])
    if not source.is_absolute() or source.is_symlink():
        raise HostReleaseBlocked("untrusted installed source")
    running = _data(state / "status.json")
    baseline = _sha(running.get("self_revision"))
    if (
        running.get("state") not in ("running", "idle")
        or _git(source, "rev-parse", "HEAD") != baseline
        or _git(source, "status", "--porcelain")
        or _git(source, "remote", "get-url", "origin") not in rollout.SOURCE_ORIGINS
    ):
        raise HostReleaseBlocked("installed baseline cannot be authenticated")
    return state, intent, running, source, original


def _clone(root: Path, name: str, revision: str) -> None:
    destination = root / name
    result = subprocess.run(
        ["/usr/bin/git", "clone", "--quiet", "--no-checkout",
         CANONICAL_SOURCE, str(destination)],
        capture_output=True, text=True, timeout=180, check=False,
    )
    if result.returncode:
        raise HostReleaseBlocked("canonical source clone failed")
    _git(destination, "checkout", "--quiet", "--detach", revision)
    if (
        _git(destination, "rev-parse", "HEAD") != revision
        or _git(destination, "status", "--porcelain")
        or _git(destination, "remote", "get-url", "origin")
        not in rollout.SOURCE_ORIGINS
    ):
        raise HostReleaseBlocked("candidate Git revision/origin mismatch")



def _stage_sidecar_python(root: Path, original_python: str) -> str:
    """Install pinned dependencies in this release, NEVER in the live Python."""
    runtime = root / "candidate-runtime"
    if runtime.exists() or runtime.is_symlink():
        raise HostReleaseBlocked("candidate runtime already exists")
    lock = root / "candidate-cli/requirements-runtime.lock"
    if lock.is_symlink() or not lock.is_file():
        raise HostReleaseBlocked("candidate runtime hash lock missing or unsafe")
    lock_digest = hashlib.sha256(lock.read_bytes()).hexdigest()
    checks = (
        [original_python, "-m", "venv", str(runtime)],
        [str(runtime / "bin/python3"), "-m", "pip", "--isolated", "install", "--no-input",
         "--disable-pip-version-check", "--no-cache-dir", "--require-hashes", "-r", str(lock)],
        [str(runtime / "bin/python3"), "-B", "-c",
         "import os,sys,mcp,httpx2; assert os.path.realpath(sys.prefix) == os.path.realpath(sys.argv[1])", str(runtime)],
    )
    for command in checks:
        try:
            completed = subprocess.run(command, capture_output=True, text=True,
                                       timeout=180, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise HostReleaseBlocked("sidecar Python creation failed") from exc
        if completed.returncode:
            raise HostReleaseBlocked("sidecar Python dependency verification failed")
    config = runtime / "pyvenv.cfg"
    if config.is_symlink() or not config.is_file():
        raise HostReleaseBlocked("sidecar Python configuration missing")
    executable = str(runtime / "bin/python3")
    _atomic(root / "candidate-python.json", {
        "version": 1, "candidate_python": executable,
        "baseline_python": original_python,
        "requirements_lock_sha256": lock_digest,
        "pyvenv_cfg_sha256": hashlib.sha256(config.read_bytes()).hexdigest(),
    })
    return executable


def _verify_candidate(root: Path, candidate: Path, python: str) -> None:
    commands = (
        [python, "scripts/verify.py", "--profile", "macos-smoke"],
        [python, "-m", "unittest", "-q", "tests.test_macos_host_service",
         "tests.test_host_supervisor_control",
         "tests.test_macos_host_self_update",
         "tests.test_macos_host_self_update_pins",
         "tests.test_macos_cli_polling_rollout"],
    )
    results = []
    for index, args in enumerate(commands):
        log = root / f"native-check-{index}.log"
        with log.open("xb") as stream:
            process = subprocess.run(
                args, cwd=candidate, stdout=stream, stderr=subprocess.STDOUT,
                check=False, timeout=1200,
            )
        if process.returncode:
            raise HostReleaseBlocked(f"native release check {index} failed; see private log")
        results.append({
            "command": args[1:],
            "exit_code": 0,
            "log_sha256": hashlib.sha256(log.read_bytes()).hexdigest(),
        })
    _atomic(root / "verified-checks.json", {
        "version": 1, "candidate_sha": _git(candidate, "rev-parse", "HEAD"),
        "checks": results,
    })


def stage(home: Path, request_id: str, *, sidecar_python: bool = False) -> dict:
    state, intent, running, source, original = _context(home, request_id)
    candidate_sha = intent["candidate_sha"]
    baseline_sha = running["self_revision"]
    if candidate_sha == baseline_sha:
        return {"status": "already_installed", "sha": baseline_sha}
    releases = state / "releases"
    _check_directory(releases)
    root = releases / candidate_sha
    root.mkdir(mode=0o700)
    _check_directory(root)
    _clone(root, "baseline-host-mvp", baseline_sha)
    _clone(root, "candidate-cli", candidate_sha)
    config = _private_file(state / "supervisor-control.json")
    backup = root / "configuration-backup"
    backup.mkdir(mode=0o700)
    (backup / "com.michal.local-agent.plist").write_bytes(original)
    (backup / "supervisor-control.json").write_bytes(config)
    for path in backup.iterdir():
        path.chmod(0o600)
    launch = plistlib.loads(original)
    original_python = launch["ProgramArguments"][0]
    python = (_stage_sidecar_python(root, original_python)
              if sidecar_python else original_python)
    _verify_candidate(root, root / "candidate-cli", python)
    if (
        _git(root / "candidate-cli", "rev-parse", "HEAD") != candidate_sha
        or _git(root / "candidate-cli", "status", "--porcelain")
    ):
        raise HostReleaseBlocked("candidate source changed during native tests")
    manifest = {
        "status": "staged_not_activated", "baseline_sha": baseline_sha,
        "candidate_sha": candidate_sha,
        "host_id": intent["target_host_id"],
        "installation_id": intent["target_installation_id"],
        "baseline_plist_sha256": hashlib.sha256(original).hexdigest(),
        "baseline_source_path": str(source),
    }
    if sidecar_python:
        manifest["candidate_python"] = python
    _atomic(root / "staging-manifest.json", manifest)
    return {"status": "verified_staged", "candidate_sha": candidate_sha}


def arm(home: Path, request_id: str) -> dict:
    state, intent, running, _source, _original = _context(home, request_id)
    candidate_sha = intent["candidate_sha"]
    baseline_sha = running["self_revision"]
    root = state / "releases" / candidate_sha
    checks = _data(root / "verified-checks.json")
    expected = (
        ["scripts/verify.py", "--profile", "macos-smoke"],
        ["-m", "unittest", "-q", "tests.test_macos_host_service",
         "tests.test_host_supervisor_control",
         "tests.test_macos_host_self_update",
         "tests.test_macos_host_self_update_pins",
         "tests.test_macos_cli_polling_rollout"],
    )
    actual = checks.get("checks")
    if (
        checks.get("version") != 1
        or checks.get("candidate_sha") != candidate_sha
        or not isinstance(actual, list)
        or len(actual) != len(expected)
        or any(
            not isinstance(item, dict)
            or item.get("exit_code") != 0
            or item.get("command") != command
            or type(item.get("log_sha256")) is not str
            or _SHA256.fullmatch(item["log_sha256"]) is None
            for item, command in zip(actual, expected)
        )
    ):
        raise HostReleaseBlocked("candidate native acceptance evidence missing")
    rollout._configure_pinned_release(baseline_sha, candidate_sha)
    prepared = rollout.prepare(home)
    armed = rollout.arm(home)
    return {"status": "armed_external_watchdog", "candidate_sha": candidate_sha,
            "prepared": prepared["state"], "watcher_armed": armed["armed"]}


UPDATER_LABEL = "dev.localagent.host-updater.v1"
UPDATER_ROOT = "Library/Application Support/local-agent/host-updater"


def enqueue_local(home: Path, request_id: str, candidate_sha: str) -> dict:
    """One-time owner-user bootstrap for an installed pre-updater generation.

    Subsequent releases use the regular pinned Host Control request, not this
    fallback. No Git publication or service process effect is performed.
    """
    if (
        sys.platform != "darwin" or os.geteuid() != os.getuid()
        or os.getuid() == 0 or not home.is_absolute()
        or _ID.fullmatch(request_id) is None
    ):
        raise HostReleaseBlocked("local bootstrap requires a valid macOS owner session")
    target = _sha(candidate_sha)
    state = home / "Library/Application Support/local-agent"
    _check_directory(state, private=False)
    identity = _data(state / "host-identity.json")
    baseline = _sha(_data(state / "status.json").get("self_revision"))
    if target == baseline:
        return {"status": "already_installed", "sha": baseline}
    intents = state / INTENTS
    try:
        intents.mkdir(mode=0o700)
    except FileExistsError:
        pass
    _check_directory(intents)
    if not isinstance(identity.get("host_id"), str) or not isinstance(
        identity.get("installation_id"), str,
    ):
        raise HostReleaseBlocked("local host identity unavailable")
    intent = {
        "version": 1,
        "request_id": request_id,
        "candidate_sha": target,
        "target_host_id": identity["host_id"],
        "target_installation_id": identity["installation_id"],
        "status": "pending_external_controller",
    }
    _atomic(intents / (request_id + ".json"), intent)
    return {"status": "queued_external_controller", "candidate_sha": target}


def _finish_verified_intent(home: Path, request_id: str, candidate_sha: str) -> None:
    """Remember a *verified installed* revision without mutating its intent.

    An old request is not pending after a later successful update.  It may
    only be retired while that exact source is authenticated as installed.
    """
    state, intent, running, _source, installed_plist = _context(home, request_id)
    if (intent["candidate_sha"] != candidate_sha
            or running["self_revision"] != candidate_sha):
        raise HostReleaseBlocked("cannot retire unverified host update intent")
    journal = state / "releases" / candidate_sha / rollout.JOURNAL
    if journal.exists() and _data(journal).get("state") != "accepted":
        raise HostReleaseBlocked("candidate cutover is not natively accepted")
    completed = state / COMPLETED
    completed.mkdir(mode=0o700, exist_ok=True)
    _check_directory(completed)
    evidence = {
        "version": 1,
        "request_id": request_id,
        "candidate_sha": candidate_sha,
        "target_host_id": intent["target_host_id"],
        "target_installation_id": intent["target_installation_id"],
        "status": "installed_verified",
    }
    path = completed / (request_id + ".json")
    try:
        _atomic(path, evidence)
    except FileExistsError:
        if _data(path) != evidence:
            raise HostReleaseBlocked("conflicting completed host update evidence")
    from scripts.macos_host_service import remember_accepted

    remember_accepted(home, candidate_sha, installed_plist)


def _watch_intents(home: Path) -> dict:
    """One externally scheduled turn. Never relaunch/replay an uncertain cutover."""
    state = home / "Library/Application Support/local-agent"
    _check_directory(state, private=False)
    intents = state / INTENTS
    if not intents.exists() and not intents.is_symlink():
        return {"status": "no_pending_intents"}
    _check_directory(intents)
    records: list[tuple[str, dict]] = []
    for entry in intents.iterdir():
        if len(records) >= 128 or _ID.fullmatch(entry.stem) is None or entry.suffix != ".json":
            raise HostReleaseBlocked("unsafe or unbounded host update intent inbox")
        item = _data(entry)
        if (
            item.get("request_id") != entry.stem
            or item.get("status") != "pending_external_controller"
        ):
            raise HostReleaseBlocked("untrusted host update intent record")
        records.append((entry.stem, item))
    if not records:
        return {"status": "no_pending_intents"}
    current = _sha(_data(state / "status.json").get("self_revision"))
    completed = state / COMPLETED
    if completed.exists() or completed.is_symlink():
        _check_directory(completed)
    unresolved = []
    for ident, item in records:
        candidate = _sha(item.get("candidate_sha"))
        receipt = completed / (ident + ".json")
        if receipt.exists() or receipt.is_symlink():
            expected = {
                "version": 1, "request_id": ident,
                "candidate_sha": candidate,
                "target_host_id": item.get("target_host_id"),
                "target_installation_id": item.get("target_installation_id"),
                "status": "installed_verified",
            }
            if _data(receipt) != expected:
                raise HostReleaseBlocked("untrusted completed host update evidence")
        elif candidate == current:
            _finish_verified_intent(home, ident, candidate)
        else:
            unresolved.append((ident, item))
    if not unresolved:
        return {"status": "verified_update_history", "count": len(records),
                "running_revision": current}
    if len(unresolved) != 1:
        raise HostReleaseBlocked("multiple pinned releases require explicit reconciliation")
    request_id, item = unresolved[0]
    candidate = _sha(item.get("candidate_sha"))
    release = state / "releases" / candidate
    if release.exists() or release.is_symlink():
        journal = release / rollout.JOURNAL
        if journal.exists() and not journal.is_symlink():
            return {"status": "external_rollout_owns_release", "candidate_sha": candidate}
        if (release / "staging-manifest.json").is_file():
            return arm(home, request_id)
        raise HostReleaseBlocked("incomplete stage retained for operator inspection")
    stage(home, request_id)
    return arm(home, request_id)


def watch(home: Path) -> dict:
    """Fail-closed external watcher, never executed inside the Local Agent."""
    if sys.platform != "darwin" or not home.is_absolute():
        raise HostReleaseBlocked("macOS watcher requires a real absolute home")
    controller = home / UPDATER_ROOT
    _check_directory(controller)
    lock = controller / "watch.lock"
    descriptor = os.open(
        lock, os.O_RDWR | os.O_CREAT | os.O_NONBLOCK
        | getattr(os, "O_NOFOLLOW", 0), 0o600,
    )
    previous_sigterm = signal.getsignal(signal.SIGTERM)
    previous_sigint = signal.getsignal(signal.SIGINT)
    def stop_external_turn(_signum, _frame):
        global _service_stop_pending
        if _defer_service_stop:
            _service_stop_pending = True
            return
        raise SystemExit(143)
    signal.signal(signal.SIGTERM, stop_external_turn)
    signal.signal(signal.SIGINT, stop_external_turn)
    try:
        info = os.fstat(descriptor)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_mode & 0o077):
            raise HostReleaseBlocked("foreign update watcher lock")
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {"status": "another_updater_turn_active"}
        from scripts import macos_host_service as service

        results = {}
        # A malformed task/project mailbox must not suppress service diagnosis.
        def service_tick() -> None:
            try:
                results["service"] = service.poll(home)
            except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as exc:
                results["service"] = {"status": "degraded", "error": f"{type(exc).__name__}: {exc}"}

        service_tick()
        results["updates"] = _run_update_turn(home, service_tick)
        if (home / "Library/Application Support/local-agent" / service.ACCEPTED).exists():
            try:
                results["recovery"] = service.recover(home)
            except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as exc:
                results["recovery"] = {"status": "degraded", "error": f"{type(exc).__name__}: {exc}"}
        return results
    finally:
        signal.signal(signal.SIGTERM, previous_sigterm)
        signal.signal(signal.SIGINT, previous_sigint)
        os.close(descriptor)


def _run_update_turn(home: Path, service_tick) -> dict:
    """Keep service reads alive while the existing release validator runs.

    One bounded child does release preparation only; it cannot execute tasks.
    Its process group is reaped on timeout/interruption before releasing the
    updater ownership lock. There is no additional scheduler or daemon.
    """
    global _defer_service_stop, _service_stop_pending
    command = [sys.executable, "-B", "-m", "scripts.macos_host_self_update",
               "update-turn", "--home", str(home)]
    root = home / UPDATER_ROOT
    output = root / "update-turn.out"
    with output.open("w", encoding="utf-8") as log:
        output.chmod(0o600)
        child = None
        try:
            _defer_service_stop = True
            child = subprocess.Popen(command, cwd=root, stdout=log, stderr=subprocess.STDOUT,
                                     start_new_session=True)
            # Pending termination is delivered only after the child is owned
            # by this finally block, closing the spawn/cleanup race.
            _defer_service_stop = False
            if _service_stop_pending:
                raise SystemExit(143)
            deadline = time.monotonic() + UPDATE_TURN_TIMEOUT_SECONDS
            next_tick = time.monotonic() + SERVICE_POLL_SECONDS
            while child.poll() is None:
                if time.monotonic() >= deadline:
                    raise TimeoutError("bounded external release preparation expired")
                if output.stat().st_size > 65536:
                    raise HostReleaseBlocked("unbounded external release preparation output")
                if time.monotonic() >= next_tick:
                    service_tick()
                    next_tick = time.monotonic() + SERVICE_POLL_SECONDS
                time.sleep(0.25)
        finally:
            _defer_service_stop = True
            try:
                if child is not None and child.poll() is None:
                    os.killpg(child.pid, signal.SIGTERM)
                    try:
                        child.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        os.killpg(child.pid, signal.SIGKILL)
                        child.wait(timeout=5)
            finally:
                _defer_service_stop = False
    if _service_stop_pending:
        _service_stop_pending = False
        raise SystemExit(143)
    if child.returncode:
        return {"status": "degraded", "exit_code": child.returncode,
                "log": str(output)}
    if output.stat().st_size > 8192:
        raise HostReleaseBlocked("oversized release preparation response")
    return json.loads(output.read_text())


def bootstrap(home: Path) -> dict:
    """One-time owner-user registration of the release-only independent poller."""
    if (
        sys.platform != "darwin" or os.geteuid() != os.getuid()
        or os.getuid() == 0 or not home.is_absolute() or home.is_symlink()
    ):
        raise HostReleaseBlocked("updater bootstrap requires non-root macOS user")
    _check_directory(home / "Library/Application Support/local-agent", private=False)
    launch_agents = home / "Library/LaunchAgents"
    agents_info = launch_agents.lstat()
    if (
        not stat.S_ISDIR(agents_info.st_mode)
        or agents_info.st_uid != os.getuid()
        or agents_info.st_mode & 0o022
    ):
        raise HostReleaseBlocked("untrusted LaunchAgents directory")
    root = home / UPDATER_ROOT
    root.mkdir(mode=0o700)
    package = root / "scripts"
    package.mkdir(mode=0o700)
    source = Path(__file__).resolve()
    sibling = source.with_name("macos_cli_polling_rollout.py")
    if not sibling.is_file() or sibling.is_symlink():
        raise HostReleaseBlocked("independent rollout source missing")
    for name, data in (
        ("__init__.py", b""),
        ("macos_host_self_update.py", source.read_bytes()),
        ("macos_cli_polling_rollout.py", sibling.read_bytes()),
        ("macos_host_service.py", source.with_name("macos_host_service.py").read_bytes()),
    ):
        target = package / name
        descriptor = os.open(
            target, os.O_CREAT | os.O_EXCL | os.O_WRONLY
            | getattr(os, "O_NOFOLLOW", 0), 0o600,
        )
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
    watcher = launch_agents / (UPDATER_LABEL + ".plist")
    command = [
        sys.executable, "-B", "-m", "scripts.macos_host_self_update",
        "watch", "--home", str(home),
    ]
    job = {
        "Label": UPDATER_LABEL,
        "ProgramArguments": command,
        "WorkingDirectory": str(root),
        "RunAtLoad": True,
        "KeepAlive": False,
        "StartInterval": 60,
        "EnvironmentVariables": {"PYTHONDONTWRITEBYTECODE": "1"},
        "ExitTimeOut": 15,
        "StandardOutPath": str(root / "watch.out"),
        "StandardErrorPath": str(root / "watch.err"),
    }
    raw = plistlib.dumps(job, fmt=plistlib.FMT_XML)
    descriptor = os.open(
        watcher, os.O_CREAT | os.O_EXCL | os.O_WRONLY
        | getattr(os, "O_NOFOLLOW", 0), 0o600,
    )
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())
    result = subprocess.run(
        ["/bin/launchctl", "bootstrap", f"gui/{os.getuid()}", str(watcher)],
        capture_output=True, text=True, timeout=30, check=False,
    )
    if result.returncode:
        raise HostReleaseBlocked("independent updater launchd bootstrap failed; preserve evidence")
    return {"status": "external_updater_registered", "label": UPDATER_LABEL}


def _reload_updater(root: Path, watcher: Path) -> None:
    target = f"gui/{os.getuid()}/{UPDATER_LABEL}"
    baseline = "/__local_agent_unselected_updater__"
    candidate = str(root)
    rollout._bootout_and_wait(target, baseline, candidate)
    rollout._bootstrap_with_retry(f"gui/{os.getuid()}", watcher, target,
                                 expected="candidate", baseline=baseline, candidate=candidate)


def refresh(home: Path) -> dict:
    """Upgrade the existing service controller only from an accepted release."""
    from scripts import macos_host_service as service

    source = Path(__file__).resolve().parents[1]
    revision = _sha(_git(source, "rev-parse", "HEAD"))
    if _git(source, "status", "--porcelain"):
        raise HostReleaseBlocked("controller refresh requires clean accepted source")
    service.adopt_accepted_release(home, revision)
    observed = service.observation(home)
    if not all(observed.get(key) for key in (
        "definition_matches", "revision_matches", "heartbeat_fresh", "generation_matches",
    )):
        raise HostReleaseBlocked("controller refresh requires the actual accepted running generation")
    root = home / UPDATER_ROOT
    _check_directory(root)
    watcher = home / "Library/LaunchAgents" / (UPDATER_LABEL + ".plist")
    old_job = _installed_plist(watcher)
    job = plistlib.loads(old_job)
    if job.get("Label") != UPDATER_LABEL or job.get("WorkingDirectory") != str(root):
        raise HostReleaseBlocked("foreign updater registration")
    with (root / "watch.lock").open("r+") as lease:
        fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
        saved = {}
        for name in ("macos_host_self_update.py", "macos_host_service.py", "macos_cli_polling_rollout.py"):
            target = root / "scripts" / name
            saved[name] = _private_file(target, max_bytes=131072) if target.exists() else None
        incoming = {name: (source / "scripts" / name).read_bytes() for name in saved}
        if saved == incoming and job.get("ExitTimeOut") == 15:
            return {"status": "independent_service_controller_current", "revision": revision}
        backup = home / "Library/Application Support/local-agent/archives" / ("service-updater-" + revision)
        if backup.exists():
            _check_directory(backup)
            if (_private_file(backup / "watcher.plist", max_bytes=65536) != old_job
                    or any((backup / name).exists() if raw is None else
                           _private_file(backup / name, max_bytes=131072) != raw
                           for name, raw in saved.items())):
                raise HostReleaseBlocked("existing updater rollback does not match this retry")
        else:
            backup.mkdir(mode=0o700)
            for name, raw in saved.items():
                if raw is not None:
                    rollout._atomic(backup / name, raw)
            rollout._atomic(backup / "watcher.plist", old_job)
        try:
            for name in saved:
                rollout._atomic(root / "scripts" / name, incoming[name])
            job["ExitTimeOut"] = 15
            rollout._atomic(watcher, plistlib.dumps(job, fmt=plistlib.FMT_XML))
            _reload_updater(root, watcher)
        except BaseException:
            for name, raw in saved.items():
                if raw is None:
                    (root / "scripts" / name).unlink(missing_ok=True)
                else:
                    rollout._atomic(root / "scripts" / name, raw)
            rollout._atomic(watcher, old_job)
            _reload_updater(root, watcher)
            raise
    return {"status": "independent_service_controller_refreshed", "revision": revision,
            "backup": str(backup)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Pinned independent Host Control update")
    parser.add_argument(
        "action", choices=("stage", "arm", "watch", "bootstrap", "refresh", "enqueue-local", "status", "recover", "adopt-accepted", "update-turn"),
    )
    parser.add_argument("--home", type=Path, required=True)
    parser.add_argument("--request-id")
    parser.add_argument("--candidate-sha")
    options = parser.parse_args(argv)
    if options.action == "enqueue-local":
        if not options.request_id or not options.candidate_sha:
            parser.error("enqueue-local needs --request-id and --candidate-sha")
        result = enqueue_local(options.home, options.request_id, options.candidate_sha)
    elif options.action in ("stage", "arm"):
        if not options.request_id or options.candidate_sha:
            parser.error("stage/arm need only --request-id")
        result = (stage if options.action == "stage" else arm)(
            options.home, options.request_id,
        )
    elif options.action == "adopt-accepted":
        if options.request_id or not options.candidate_sha:
            parser.error("adopt-accepted requires only --candidate-sha")
        from scripts.macos_host_service import adopt_accepted_release

        result = adopt_accepted_release(options.home, options.candidate_sha)
    elif options.action == "update-turn":
        if options.request_id or options.candidate_sha:
            parser.error("update-turn does not accept request parameters")
        result = _watch_intents(options.home)
    elif options.action in ("status", "recover"):
        if options.request_id or options.candidate_sha:
            parser.error("status/recover do not accept request parameters")
        from scripts import macos_host_service as service

        result = (service.observation if options.action == "status" else service.recover)(options.home)
    else:
        if options.request_id or options.candidate_sha:
            parser.error("watch/bootstrap do not accept request parameters")
        result = {"watch": watch, "bootstrap": bootstrap, "refresh": refresh}[options.action](options.home)
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
