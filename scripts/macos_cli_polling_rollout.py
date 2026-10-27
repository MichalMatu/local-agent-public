"""Standalone Mac CLI polling rollout and independent recovery to working host MVP.

The source is copied outside the candidate checkout and runs as a dedicated
launchd job, never inside the Local Agent worker it restarts. No second task
transport/worker, root helper, per-command grants, or operator ref edits.
"""
from __future__ import annotations

import argparse
from contextlib import ExitStack
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import plistlib
import re
import subprocess
import sys
import time
import tempfile

BASE_SHA = "7df83dcd998e23b30a5c18ed6d347772aa3e5d98"
CANDIDATE_SHA = "PIN_CANDIDATE_SHA_AT_STAGING"
RELEASE_NAME = "Library/Application Support/local-agent/releases/" + CANDIDATE_SHA
_DYNAMIC_PIN = False


def _configure_pinned_release(base_sha: str, candidate_sha: str) -> None:
    """Set exact SHA pins for this independent controller process.

    The pins are passed to its persisted launchd watch argv as well; never
    infer them by following a moving branch after a reboot.
    """
    global BASE_SHA, CANDIDATE_SHA, RELEASE_NAME, CONTROLLER_LABEL, _DYNAMIC_PIN
    if (
        not isinstance(base_sha, str)
        or not isinstance(candidate_sha, str)
        or re.fullmatch(r"[0-9a-f]{40}", base_sha) is None
        or re.fullmatch(r"[0-9a-f]{40}", candidate_sha) is None
        or base_sha == candidate_sha
    ):
        raise CutoverBlocked("pinned release requires two distinct exact SHA-1 commits")
    BASE_SHA = base_sha
    CANDIDATE_SHA = candidate_sha
    RELEASE_NAME = "Library/Application Support/local-agent/releases/" + CANDIDATE_SHA
    CONTROLLER_LABEL = "dev.localagent.cli-rollout." + CANDIDATE_SHA[:12]
    _DYNAMIC_PIN = True


def _pin_arguments() -> list[str]:
    return ["--base-sha", BASE_SHA, "--candidate-sha", CANDIDATE_SHA] if _DYNAMIC_PIN else []
HOST_ID = "example-mac"
INSTALL_ID = "e6610a5b-cb49-4027-83f2-2dcfdc4a66be"
LABEL = "com.michal.local-agent"
JOURNAL = "mvp-cutover.json"
FROZEN = "macos-release-admission-frozen.json"
LOCK = "macos-release-admission.lock"
CONTROLLER_LABEL = "dev.localagent.cli-rollout." + CANDIDATE_SHA[:8]
SOURCE_ORIGINS = (
    "https://github.com/MichalMatu/local-agent-public",
    "https://github.com/MichalMatu/local-agent-public.git",
    "git@github.com:MichalMatu/local-agent-public",
    "git@github.com:MichalMatu/local-agent-public.git",
)


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
    baseline = root / "baseline-host-mvp"
    candidate = root / "candidate-cli"
    sources = ((baseline, BASE_SHA), (candidate, CANDIDATE_SHA)) if require_candidate else ((baseline, BASE_SHA),)
    for path, revision in sources:
        if path.is_symlink() or not path.is_dir() or not (path / "agent_entrypoint.py").is_file():
            raise CutoverBlocked("staged release checkout unavailable")
        if _git(path, "rev-parse", "HEAD") != revision or _git(path, "status", "--porcelain"):
            raise CutoverBlocked("staged release source drift")
        if _git(path, "remote", "get-url", "origin") not in SOURCE_ORIGINS:
            raise CutoverBlocked("staged release origin is not the canonical GitHub repository")
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



def _validated_candidate_python(root: Path, manifest: dict, previous: str) -> str:
    """A sidecar is optional only for older manifests; declared venv is mandatory."""
    if "candidate_python" not in manifest:
        return previous
    target = root / "candidate-runtime" / "bin" / "python3"
    record_path = root / "candidate-python.json"
    record = _json(record_path)
    config = root / "candidate-runtime" / "pyvenv.cfg"
    lock = root / "candidate-cli/requirements-runtime.lock"
    if (
        type(manifest["candidate_python"]) is not str
        or manifest["candidate_python"] != str(target)
        or record.get("version") != 1
        or record.get("candidate_python") != str(target)
        or record.get("baseline_python") != previous
        or lock.is_symlink() or not lock.is_file()
        or record.get("requirements_lock_sha256") != hashlib.sha256(lock.read_bytes()).hexdigest()
        or config.is_symlink() or not config.is_file()
        or record.get("pyvenv_cfg_sha256") != hashlib.sha256(config.read_bytes()).hexdigest()
        or not target.is_file() or not os.access(target, os.X_OK)
    ):
        raise CutoverBlocked("candidate Python release identity mismatch")
    result = subprocess.run(
        [str(target), "-B", "-c",
         "import os,sys,mcp,httpx2; assert os.path.realpath(sys.prefix) == os.path.realpath(sys.argv[1])",
         str(root / "candidate-runtime")],
        capture_output=True, text=True, timeout=12, check=False,
    )
    if result.returncode:
        raise CutoverBlocked("candidate Python imports unavailable")
    return str(target)


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
        raise CutoverBlocked("live working host MVP plist no longer equals backup")
    document = _read_plist(baseline_file, digest)
    argv = document.get("ProgramArguments")
    if (not isinstance(argv, list) or len(argv) != 6
            or argv[1] != manifest["baseline_source_path"] + "/agent_entrypoint.py"
            or document.get("WorkingDirectory") != manifest["baseline_source_path"]
            or document.get("EnvironmentVariables", {}).get("LOCAL_AGENT_MACOS_ADMISSION_REQUIRED") != "1"):
        raise CutoverBlocked("installed host MVP launchd policy mismatch")
    replacement = dict(document)
    new_argv = list(argv)
    new_argv[0] = _validated_candidate_python(root, manifest, argv[0])
    new_argv[1] = str(candidate / "agent_entrypoint.py")
    replacement["ProgramArguments"] = new_argv
    replacement["WorkingDirectory"] = str(candidate)
    candidate_plist = plistlib.dumps(replacement, fmt=plistlib.FMT_XML, sort_keys=True)
    _atomic(root / "mvp-candidate.plist", candidate_plist)
    _atomic(root / "mvp-baseline.plist", raw)
    service_config = state / "supervisor-control.json"
    backed_config = root / "configuration-backup/supervisor-control.json"
    if (service_config.is_symlink() or backed_config.is_symlink()
            or not service_config.is_file() or not backed_config.is_file()
            or service_config.read_bytes() != backed_config.read_bytes()):
        raise CutoverBlocked("live host intake changed; preserve accepted MVP")
    selected = _json(service_config)
    if (selected.get("enabled") is not True or selected.get("host_id") != HOST_ID
            or selected.get("host_intake") !=
            {"enabled": True, "authorization": "github_writers"}):
        raise CutoverBlocked("working host enrollment configuration invalid")
    control_checkout = Path(selected["control_checkout"])
    if (control_checkout.is_symlink() or not (control_checkout / ".git").is_dir()):
        raise CutoverBlocked("accepted host Git checkout unavailable")
    origin = _git(control_checkout, "remote", "get-url", "origin")
    if origin not in SOURCE_ORIGINS or _git(control_checkout, "branch", "--show-current") != "agent-control":
        raise CutoverBlocked("accepted host control origin or branch changed")
    _atomic(root / "mvp-host-config.json", backed_config.read_bytes())
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
                                "--home", str(home), *_pin_arguments()],
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


def _persistent_watch_path(home: Path) -> Path:
    """The release watcher is reloaded by launchd at the next GUI login."""
    directory = home / "Library/LaunchAgents"
    if (directory.is_symlink() or not directory.is_dir()
            or directory.stat().st_uid != os.getuid()):
        raise CutoverBlocked("untrusted user LaunchAgents directory")
    return directory / (CONTROLLER_LABEL + ".plist")


def _watch_job_bytes(home: Path, root: Path, record: dict) -> bytes:
    controller = root / "mvp-controller.py"
    if (controller.is_symlink() or not controller.is_file()
            or hashlib.sha256(controller.read_bytes()).hexdigest()
            != record.get("controller_sha256")):
        raise CutoverBlocked("independent watcher source drift")
    source = root / "mvp-watch.plist"
    if source.is_symlink() or not source.is_file():
        raise CutoverBlocked("independent watcher plist missing")
    data = source.read_bytes()
    try:
        job = plistlib.loads(data)
    except (ValueError, TypeError) as exc:
        raise CutoverBlocked("invalid independent watcher plist") from exc
    if (not isinstance(job, dict) or job.get("Label") != CONTROLLER_LABEL
            or job.get("ProgramArguments") != [
                sys.executable, "-B", str(controller), "watch", "--home", str(home),
                *_pin_arguments(),
            ]
            or job.get("WorkingDirectory") != str(root)
            or job.get("RunAtLoad") is not True
            or job.get("KeepAlive") is not False
            or job.get("StartInterval") != 20):
        raise CutoverBlocked("untrusted independent watcher job")
    return data


def arm(home: Path) -> dict:
    """Explicitly persist the watchdog; inert prepare never enables login startup."""
    root, state, candidate, plist = _validate_paths(home)
    record = _load_state(root)
    if (record.get("state") != "prepared"
            or record.get("candidate_sha") != CANDIDATE_SHA
            or record.get("baseline_sha") != BASE_SHA):
        raise CutoverBlocked("only a prepared release may be armed")
    if (plist.read_bytes() != (root / "mvp-baseline.plist").read_bytes()
            or (state / "supervisor-control.json").read_bytes()
            != (root / "mvp-host-config.json").read_bytes()):
        raise CutoverBlocked("live baseline changed since release preparation")
    if (_frozen_path(state).exists() or _frozen_path(state).is_symlink()):
        raise CutoverBlocked("another release freeze is active")
    content = _watch_job_bytes(home, root, record)
    target = _persistent_watch_path(home)
    # An explicit arm is a no-clobber transaction. The copied file remains
    # after a reboot, unlike an ephemeral launchctl bootstrap of the staging copy.
    try:
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL |
                     getattr(os, "O_NOFOLLOW", 0), 0o600)
    except FileExistsError as exc:
        raise CutoverBlocked("persistent watcher path already occupied") from exc
    with os.fdopen(fd, "wb") as stream:
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())
    parent = os.open(target.parent, os.O_RDONLY)
    try:
        os.fsync(parent)
    finally:
        os.close(parent)
    _launchctl("bootstrap", f"gui/{os.getuid()}", str(target))
    return {"state": "prepared", "armed": True, "persistent_job": str(target)}


def retire(home: Path) -> dict:
    """Uninstall only this completed release's own persistent watch job."""
    root, state, candidate, plist = _validate_paths(home, require_candidate=False)
    record = _load_state(root)
    status = record.get("state")
    if status not in ("accepted", "rolled_back"):
        raise CutoverBlocked("release is not terminal; preserve reboot recovery")
    if (_frozen_path(state).exists() or _frozen_path(state).is_symlink()):
        raise CutoverBlocked("release freeze is still active")
    expected = _watch_job_bytes(home, root, record)
    target = _persistent_watch_path(home)
    if target.is_symlink() or not target.is_file() or target.read_bytes() != expected:
        raise CutoverBlocked("persistent watcher missing or changed; no removal")
    manifest = _json(root / "staging-manifest.json")
    baseline = manifest["baseline_source_path"] + "/agent_entrypoint.py"
    generation = _generation(
        f"gui/{os.getuid()}/{LABEL}", baseline,
        str(candidate / "agent_entrypoint.py"),
    )
    if generation != ("candidate" if status == "accepted" else "baseline"):
        raise CutoverBlocked("release journal and running source disagree")
    live = _launchctl("print", f"gui/{os.getuid()}/{LABEL}", acceptable=(0, 113))
    if (live.returncode != 0 or "state = running" not in live.stdout
            or "pid =" not in live.stdout):
        raise CutoverBlocked("terminal release has no running production service")
    supervisor = _json(state / "status.json")
    stamp = datetime.fromisoformat(supervisor["updated_at"])
    age = (datetime.now(timezone.utc) - stamp).total_seconds()
    current_sha = CANDIDATE_SHA if status == "accepted" else BASE_SHA
    if (stamp.tzinfo is None or not 0 <= age < 45
            or supervisor.get("self_revision") != current_sha
            or supervisor.get("state") not in ("running", "idle")):
        raise CutoverBlocked("terminal release has no healthy current heartbeat")
    launch_target = f"gui/{os.getuid()}/{CONTROLLER_LABEL}"
    running = _launchctl("print", launch_target, acceptable=(0, 113))
    if running.returncode == 0:
        if str(root / "mvp-controller.py") not in running.stdout:
            raise CutoverBlocked("watcher registration is not the expected release")
        _launchctl("bootout", launch_target)
        if _launchctl("print", launch_target, acceptable=(0, 113)).returncode != 113:
            raise CutoverBlocked("watcher remained registered after bootout")
    if target.is_symlink() or target.read_bytes() != expected:
        raise CutoverBlocked("persistent watcher changed during retirement")
    target.unlink()
    parent = os.open(target.parent, os.O_RDONLY)
    try:
        os.fsync(parent)
    finally:
        os.close(parent)
    return {"state": status, "retired": True, "persistent_job_removed": True}


def _queue_idle(state: Path) -> bool:
    status = _json(state / "status.json")
    if status.get("state") not in ("running", "idle") or status.get("self_revision") != BASE_SHA:
        return False
    stamp = datetime.fromisoformat(status["updated_at"])
    if stamp.tzinfo is None or abs((datetime.now(timezone.utc) - stamp).total_seconds()) > 100:
        return False
    if status.get("current_task_id") is not None or status.get("active_repository_ids"):
        return False
    registry = _json(state / "repositories.json")
    entries = registry.get("repositories")
    if (type(registry.get("version")) is not int
            or registry["version"] != 1 or not isinstance(entries, list)):
        raise CutoverBlocked("unexpected repository registry at release drain")
    # Host-only installations and large changing project pools are valid.
    # Check EVERY registered queue, including disabled repositories.
    ids: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict):
            raise CutoverBlocked("invalid repository entry at release drain")
        ident = entry.get("id")
        flag = entry.get("enabled", True)
        if (not isinstance(ident, str)
                or not re.fullmatch(r"[A-Za-z0-9._-]+", ident)
                or ident in (".", "..")
                or ident.casefold() in ids or type(flag) is not bool):
            raise CutoverBlocked("ambiguous repository registry at release drain")
        ids.add(ident.casefold())
    # Durable terminal publication is not execution. The subsequent exclusive
    # admission lease blocks live workers/descendants; do not strand updates
    # behind a failed project's preserved result spool or historical claims.
    for entry in entries:
        ident = entry["id"]
        for leaf in ("claims", "result-spool", "corrupt-claims"):
            path = state / "repositories" / ident / leaf
            if not path.is_dir() and leaf != "corrupt-claims":
                raise CutoverBlocked("missing release ledger")
            if path.is_symlink():
                raise CutoverBlocked("symlinked release ledger")
    for leaf in ("claims", "result-spool"):
        path = state / leaf
        if path.is_symlink() or not path.is_dir():
            raise CutoverBlocked("missing or symlinked global release ledger")
    return True


def _execution_fence(home: Path, state: Path, source: Path) -> ExitStack:
    """Fence registered and historical descendants before a live cutover.

    Only a validated prepared candidate reaches this import. Rollback of an
    interrupted switch remains standalone and never depends on candidate code.
    """
    sys.path.insert(0, str(source))
    try:
        from scripts.macos_host_service import _registered_leases, _lease
        paths = set(_registered_leases(home, source))
        paths.update((state / "locks/repositories").glob("*.lock"))
        if len(paths) > 4096:
            raise CutoverBlocked("unbounded execution lease inventory")
        leases = ExitStack()
        try:
            for path in sorted(paths):
                _lease(leases, path, create=True)
        except BaseException:
            leases.close()
            raise
        return leases
    finally:
        sys.path.pop(0)


def _frozen_path(state: Path) -> Path:
    return state / FROZEN


def _drain_record() -> dict:
    return {"version": 1, "state": "draining", "baseline_sha": BASE_SHA,
            "candidate_sha": CANDIDATE_SHA}


def _drain_admission(state: Path) -> None:
    """Fence new turns while already admitted workers finish normally."""
    marker = _frozen_path(state)
    try:
        fd = os.open(marker, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
    except FileExistsError:
        if _json(marker) != _drain_record() or marker.stat().st_mode & 0o077:
            raise CutoverBlocked("another release freeze is active")
        return
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        stream.write(json.dumps(_drain_record(), sort_keys=True) + "\n")
        stream.flush()
        os.fsync(stream.fileno())
    parent = os.open(state, os.O_RDONLY)
    try:
        os.fsync(parent)
    finally:
        os.close(parent)


def _freeze(state: Path) -> int:
    lock = state / LOCK
    if lock.is_symlink() or not lock.is_file():
        raise CutoverBlocked("live release admission lock absent")
    fd = os.open(lock, os.O_RDWR | getattr(os, "O_NOFOLLOW", 0))
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        marker = _frozen_path(state)
        if _json(marker) != _drain_record() or marker.stat().st_mode & 0o077:
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
    candidate = str(root / "candidate-cli" / "agent_entrypoint.py")
    target = f"gui/{os.getuid()}/{LABEL}"
    domain = f"gui/{os.getuid()}"
    expected = record["plist_baseline_sha256"]
    _read_plist(root / "mvp-baseline.plist", expected)
    backup = root / "baseline-host-mvp"
    if (_git(backup, "rev-parse", "HEAD") != BASE_SHA
            or _git(backup, "status", "--porcelain")):
        raise CutoverBlocked("trusted independent B1c backup changed")
    # No new host enrollment is created: keep the working Git inbox byte-for-byte.
    config = state / "supervisor-control.json"
    backup_config = root / "configuration-backup/supervisor-control.json"
    if (config.is_symlink() or not config.is_file()
            or backup_config.is_symlink() or not backup_config.is_file()
            or config.read_bytes() != backup_config.read_bytes()):
        raise CutoverBlocked("host intake changed; preserve freeze for operator inspection")
    current_hash = hashlib.sha256(plist.read_bytes()).hexdigest()
    if current_hash not in (record["plist_baseline_sha256"], record["plist_candidate_sha256"]):
        raise CutoverBlocked("installed plist modified outside release; leave frozen")
    marker = _frozen_path(state)
    if not marker.is_file() or marker.read_bytes() != b'{"state":"frozen"}\n':
        raise CutoverBlocked("trusted release freeze missing; leave frozen")
    # Keep admission frozen until the previous live host MVP is healthy.
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
    _drain_admission(state)
    if not _queue_idle(state):
        return {"state": "prepared", "waiting_for_safe_drain": True}
    try:
        fd = _freeze(state)
    except BlockingIOError:
        # An existing project task legitimately holds a shared admission
        # lease. Do not throw a traceback or try a competing cutover.
        return {"state": "prepared", "waiting_for_active_task_lease": True}
    execution = None
    try:
        if not _queue_idle(state):
            return {"state": "prepared", "waiting_for_safe_drain": True}
        try:
            execution = _execution_fence(home, state, candidate_dir)
        except BlockingIOError:
            return {"state": "prepared", "waiting_for_execution_leases": True}
        if _generation(target, baseline, candidate) != "baseline":
            raise CutoverBlocked("running source is not the accepted host MVP")
        if plist.read_bytes() != (root / "mvp-baseline.plist").read_bytes():
            raise CutoverBlocked("installed host MVP launchd plist drift")
        # Durable journal and freeze before the first switch effect.
        _atomic(_frozen_path(state), b'{"state":"frozen"}\n')
        record = _save_state(root, record, "switching")
        try:
            _atomic(plist, (root / "mvp-candidate.plist").read_bytes())
            _bootout_and_wait(target, baseline, candidate)
            execution.close()
            execution = None
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
            if execution is not None:
                execution.close()
                execution = None
            _restore(root, state, plist, record)
            raise
    finally:
        if execution is not None:
            execution.close()
        os.close(fd)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Externally recovered Mac MVP cutover")
    parser.add_argument("action", choices=("prepare", "arm", "watch", "retire"))
    parser.add_argument("--home", type=Path, required=True)
    parser.add_argument("--base-sha")
    parser.add_argument("--candidate-sha")
    opts = parser.parse_args(argv)
    if (opts.base_sha is None) != (opts.candidate_sha is None):
        raise CutoverBlocked("both release SHAs are required together")
    if opts.base_sha is not None:
        _configure_pinned_release(opts.base_sha, opts.candidate_sha)
    actions = {"prepare": prepare, "arm": arm, "watch": watch, "retire": retire}
    result = actions[opts.action](opts.home)
    if not result.get("idempotent"):
        print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
