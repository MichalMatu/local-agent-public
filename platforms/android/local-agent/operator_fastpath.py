"""One independent, owner-operated Termux fast-path release; never via Git tasks.

Create a new immutable source bundle, verify old runit/rollback and drain, then
switch only the private Termux Agent runit service. Other services are untouched.
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time

from package import package

BASELINE = "f0bd5f552228229bb5fb6644facca1ac1c2c7f80"
BASELINE_RUN_SHA = "a3c6a1bc913709679be2c837a04269aa0c65aa3d419d24b8f699f27b4f105768"
HOST = "example-termux"
INSTALLATION = "3fd30ca4-42f8-4d74-9348-7c10d36db00f"
RUN_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
POLL_FLAG = b"--poll-seconds 30"


class Refused(RuntimeError):
    pass


def checked(*args: str, timeout: int = 30, env: dict | None = None) -> str:
    try:
        result = subprocess.run(args, env=env, text=True, capture_output=True,
                                timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise Refused("external command failed: " + args[0]) from exc
    if result.returncode != 0:
        raise Refused("external command refused: " + args[0] + " " + args[1])
    return result.stdout.strip()


def fsync_dir(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def atomic(path: Path, data: bytes, *, mode: int = 0o600) -> None:
    if path.is_symlink():
        raise Refused("unsafe target symlink")
    fd, temp = tempfile.mkstemp(prefix=".release-fastpath-", dir=str(path.parent))
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
        fsync_dir(path.parent)
    finally:
        if os.path.lexists(temp):
            os.unlink(temp)


def save_json(path: Path, payload: dict) -> None:
    atomic(path, (json.dumps(payload, sort_keys=True) + "\n").encode())


def read_json(path: Path) -> dict:
    if path.is_symlink() or not path.is_file():
        raise Refused("missing/unsafe private receipt: " + path.name)
    obj = json.loads(path.read_text())
    if not isinstance(obj, dict):
        raise Refused("malformed private receipt")
    return obj


def ensure_private(path: Path, *, file: bool = False) -> None:
    if path.is_symlink() or not (path.is_file() if file else path.is_dir()):
        raise Refused("missing or unsafe private path: " + path.name)
    st = path.stat()
    if st.st_uid != os.getuid() or st.st_mode & 0o077:
        raise Refused("untrusted private ownership/permissions: " + path.name)


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def verify_bundle(root: Path, sha: str) -> None:
    ensure_private(root)
    manifest = read_json(root / "manifest.json")
    files = manifest.get("files_sha256")
    if (manifest.get("source_sha") != sha or manifest.get("platform") != "termux"
            or type(files) is not dict or len(files) != (23 if sha == BASELINE else 25)
            or (sha != BASELINE and "LICENSE" not in files)):
        raise Refused("candidate/baseline package provenance mismatch")
    for name, digest in files.items():
        if (not isinstance(name, str) or (name != "LICENSE" and not name.endswith(".py"))
                or name.startswith("/") or ".." in Path(name).parts):
            raise Refused("unsafe bundle file entry")
        p = root / name
        if p.is_symlink() or not p.is_file() or sha256(p.read_bytes()) != digest:
            raise Refused("source bundle checksum mismatch")


def operator_environment() -> Path:
    h = Path.home()
    if (not sys.stdin.isatty() or not sys.stdout.isatty()
            or os.getuid() != os.geteuid() or os.getuid() == 0
            or os.environ.get("LOCAL_AGENT_LEASE_FDS")
            or os.environ.get("LOCAL_AGENT_LEASE_KEYS_DIGEST")
            or os.environ.get("PREFIX") != "/data/data/com.termux/files/usr"
            or h != Path("/data/data/com.termux/files/home")):
        raise Refused("external interactive Termux owner terminal required")
    return h


def provenance(source: Path, expected: str) -> None:
    if not RUN_SHA_RE.fullmatch(expected):
        raise Refused("invalid candidate SHA")
    if checked("git", "-C", str(source), "rev-parse", "HEAD") != expected:
        raise Refused("exact detached source SHA mismatch")
    if checked("git", "-C", str(source), "status", "--porcelain", "--untracked-files=all"):
        raise Refused("operator source checkout not clean")
    if checked("git", "-C", str(source), "remote", "get-url", "origin") != "git@github.com:example-owner/local-agent.git":
        raise Refused("unexpected Git source origin")


def paths(home: Path) -> dict[str, Path]:
    s = home / ".local/state/local-agent/termux"
    rc = s / "release-control"
    service = s / "runit/services/termux-agent"
    return {
        "state": s, "control": rc, "service": service,
        "run": service / "run", "rollback": rc / "rollback.sh",
        "baseline": home / ".local/share/local-agent/staged" / BASELINE,
        "candidate_parent": home / ".local/share/local-agent/releases",
        "config": home / ".config/local-agent/termux.json",
        "journal": rc / "fastpath-release-v1.json",
        "candidate_run": rc / "fastpath-release-v1.run",
        "lock": rc / "fastpath-release-v1.lock",
    }


def running_status(service: Path) -> str:
    return checked("sv", "status", str(service), timeout=10).splitlines()[0]


def baseline_preflight(p: dict[str, Path]) -> bytes:
    for key in ("state", "control", "service", "candidate_parent"):
        ensure_private(p[key])
    ensure_private(p["config"], file=True)
    ensure_private(p["rollback"], file=True)
    ensure_private(p["run"], file=True)
    verify_bundle(p["baseline"], BASELINE)
    old = p["run"].read_bytes()
    if sha256(old) != BASELINE_RUN_SHA:
        raise Refused("unexpected live runit baseline; no overwrite")
    if p["state"].joinpath("EMERGENCY_STOP").exists():
        raise Refused("emergency STOP already set; cannot deploy")
    if not running_status(p["service"]).startswith("run:"):
        raise Refused("private runit service not running")
    verified = checked("sh", str(p["rollback"]), "--verify", timeout=18)
    if "INDEPENDENT_ROLLBACK_VERIFY_PASS" not in verified:
        raise Refused("independent baseline rollback not verified")
    return old


def unspooled_claims(s: Path) -> list[str]:
    result_dir = s / "results"
    return [x.stem for x in (s / "claims").glob("*.json")
            if not (result_dir / x.name).is_file()]


def candidate_run(p: dict[str, Path], candidate: Path, old: bytes) -> bytes:
    original = str(p["baseline"]).encode()
    new = str(candidate).encode()
    if old.count(original) != 1 or old.count(POLL_FLAG) != 1:
        raise Refused("unexpected runit script template/explicit poll flag")
    script = old.replace(original, new).replace(POLL_FLAG, b"--poll-seconds 10")
    if old == script or original in script or b"serve" not in script:
        raise Refused("invalid generated candidate runit script")
    return script


def stage(p: dict[str, Path], source: Path, sha: str) -> dict:
    if p["journal"].exists() or p["candidate_run"].exists():
        raise Refused("previous candidate transaction exists; inspect rather than replay")
    old = baseline_preflight(p)
    candidate = p["candidate_parent"] / sha
    if candidate.exists() or candidate.is_symlink():
        raise Refused("immutable candidate path already exists")
    package(candidate, sha)
    verify_bundle(candidate, sha)
    env = dict(os.environ)
    env["PYTHONPATH"] = str(candidate)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    checked(sys.executable, "-B", "-m", "termux_agent",
            "--config", str(p["config"]), "--state", str(p["state"]),
            "preflight", timeout=40, env=env)
    # Run the same native tests on the owner-selected physical Android.
    checked(sys.executable, "-B", "-m", "unittest", "discover",
            "-s", str(source / "platforms/android/local-agent/tests"),
            "-p", "test_termux_agent.py", "-q", timeout=130, env=env)
    run = candidate_run(p, candidate, old)
    atomic(p["candidate_run"], run, mode=0o700)
    save_json(p["journal"], {
        "version": 1, "phase": "prepared", "baseline_sha": BASELINE,
        "candidate_sha": sha, "baseline_run_sha256": sha256(old),
        "candidate_run_sha256": sha256(run),
        "candidate_manifest_sha256": sha256((candidate / "manifest.json").read_bytes()),
        "host": HOST, "installation": INSTALLATION,
    })
    return {"status": "prepared_only", "candidate_sha": sha,
            "baseline_sha": BASELINE, "native_tests": "pass",
            "service_modified": False}


def running_candidate_pid(pid: int, source: Path) -> bool:
    """Check the replacement worker actually inherited candidate PYTHONPATH.

    Termux's same-UID /proc visibility is mandatory for cutover acceptance.
    Unknown visibility is not treated as proof of the right running release.
    """
    environ = Path("/proc") / str(pid) / "environ"
    try:
        values = environ.read_bytes().split(bytes([0]))
    except (OSError, ValueError):
        return False
    return b"PYTHONPATH=" + str(source).encode() in values


def poll_state(service: Path, prefix: str, limit: float) -> str:
    deadline = time.monotonic() + limit
    while time.monotonic() < deadline:
        result = running_status(service)
        if result.startswith(prefix):
            return result
        time.sleep(.5)
    raise Refused("private runit shutdown/start timeout")


def switch(p: dict[str, Path], sha: str) -> dict:
    journal = read_json(p["journal"])
    if (journal.get("version") != 1 or journal.get("phase") != "prepared"
            or journal.get("candidate_sha") != sha
            or journal.get("baseline_sha") != BASELINE
            or journal.get("host") != HOST
            or journal.get("installation") != INSTALLATION):
        raise Refused("existing release transaction not prepared for exact target")
    baseline_preflight(p)
    candidate = p["candidate_parent"] / sha
    verify_bundle(candidate, sha)
    if sha256((candidate / "manifest.json").read_bytes()) != journal.get("candidate_manifest_sha256"):
        raise Refused("candidate manifest changed after staging")
    next_run = p["candidate_run"]
    ensure_private(next_run, file=True)
    if sha256(next_run.read_bytes()) != journal.get("candidate_run_sha256"):
        raise Refused("candidate runit script changed after staging")
    if unspooled_claims(p["state"]):
        raise Refused("unfinished command claim; cannot switch")
    old_status = running_status(p["service"])
    match = re.search(r"\(pid (\d+)\)", old_status)
    if not match:
        raise Refused("old worker PID not visible")
    old_pid = match.group(1)
    stop_file = p["state"] / "EMERGENCY_STOP"
    if stop_file.exists() or stop_file.is_symlink():
        raise Refused("emergency stop changed before switch")
    fd = os.open(stop_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(fd, b"release-cutover\n")
        os.fsync(fd)
    finally:
        os.close(fd)
    fsync_dir(p["state"])
    journal["phase"] = "stopping"
    save_json(p["journal"], journal)
    try:
        checked("sv", "down", str(p["service"]), timeout=70)
        poll_state(p["service"], "down:", 65)
        if unspooled_claims(p["state"]):
            raise Refused("unfinished claim after drain; preserving STOP")
        atomic(p["run"], next_run.read_bytes(), mode=0o700)
        journal["phase"] = "switched_unverified"
        save_json(p["journal"], journal)
        checked("sv", "up", str(p["service"]), timeout=25)
        current = poll_state(p["service"], "run:", 25)
        new_pid = re.search(r"\(pid (\d+)\)", current)
        if not new_pid or new_pid.group(1) == old_pid:
            raise Refused("new supervised PID was not observed")
        time.sleep(2)
        if not running_status(p["service"]).startswith("run:"):
            raise Refused("new worker did not remain running")
        if not running_candidate_pid(int(new_pid.group(1)), candidate):
            raise Refused("new worker source could not be verified")
        if stop_file.read_bytes() != b"release-cutover\n":
            raise Refused("STOP changed by another operator")
        os.unlink(stop_file)
        fsync_dir(p["state"])
        journal["phase"] = "running_pending_git_acceptance"
        journal["new_pid"] = int(new_pid.group(1))
        save_json(p["journal"], journal)
        return {"status": "running_pending_git_acceptance", "candidate_sha": sha,
                "new_pid": int(new_pid.group(1)), "baseline_preserved": True}
    except Exception:
        journal["phase"] = "external_recovery_required"
        save_json(p["journal"], journal)
        # The existing independent baseline controller is the recovery owner.
        # It preserves evidence and leaves emergency STOP enabled.
        try:
            checked("sh", str(p["rollback"]), "--apply", timeout=110)
        except Refused:
            pass
        raise


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("action", choices=("prepare", "switch", "inspect"))
    ap.add_argument("--candidate-sha", required=True)
    args = ap.parse_args()
    try:
        home = operator_environment()
        source = Path(__file__).resolve().parents[3]
        provenance(source, args.candidate_sha)
        p = paths(home)
        ensure_private(p["control"])
        with p["lock"].open("a+b") as handle:
            os.fchmod(handle.fileno(), 0o600)
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            if args.action == "prepare":
                value = stage(p, source, args.candidate_sha)
            elif args.action == "switch":
                value = switch(p, args.candidate_sha)
            else:
                value = read_json(p["journal"])
    except (Refused, OSError, subprocess.TimeoutExpired, ValueError, json.JSONDecodeError) as exc:
        print("TERMUX_FASTPATH_REFUSED: " + str(exc), file=sys.stderr)
        return 2
    print(json.dumps(value, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
