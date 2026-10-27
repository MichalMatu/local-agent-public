"""External launchd service restore, strictly restricted to a disposable Mac label.

This is a REAL launchctl sandbox exercise, not a production update tool.
It must never accept com.michal.local-agent, a non-temporary transaction
directory or an arbitrary plist. Source is supplied independently of the
candidate service, so a rejected candidate cannot destroy its own restorer.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import plistlib
import re
import stat
import subprocess
import sys
import tempfile

LABEL_PREFIX = "dev.localagent.macos-b1c.disposable."
MARKER = ".mac-b1c-disposable-service-recovery"
MARKER_BYTES = b"local-agent-macos-disposable-service-recovery-v1\n"
JOURNAL = "service-recovery.json"
MAX_RECORD = 4096
MAX_PLIST = 8192


class DisposableRecoveryRejected(ValueError):
    """An independent recovery cannot safely establish its target."""


def _root(path: Path) -> Path:
    if sys.platform != "darwin":
        raise DisposableRecoveryRejected("disposable launchd restore requires macOS")
    path = Path(path)
    try:
        root = path.resolve(strict=True)
        tmp = Path(tempfile.gettempdir()).resolve(strict=True)
        info = root.lstat()
    except OSError as exc:
        raise DisposableRecoveryRejected("disposable recovery root unavailable") from exc
    if (path != root or root == tmp or tmp not in root.parents
            or not root.name.startswith("la-b1c-launchd-")
            or not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
            or info.st_mode & 0o077):
        raise DisposableRecoveryRejected("recovery root is not owner-private Mac temp sandbox")
    _regular(root / MARKER, MAX_RECORD, expected=MARKER_BYTES)
    return root


def _regular(path: Path, bound: int, *, expected: bytes | None = None) -> bytes:
    if path.is_symlink():
        raise DisposableRecoveryRejected("disposable recovery input is symlinked")
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except OSError as exc:
        raise DisposableRecoveryRejected("disposable recovery input unavailable") from exc
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= bound
                or info.st_uid != os.getuid() or info.st_mode & 0o077
                or info.st_nlink != 1):
            raise DisposableRecoveryRejected("disposable recovery input is not private")
        with os.fdopen(fd, "rb", closefd=False) as handle:
            raw = handle.read(bound + 1)
    finally:
        os.close(fd)
    if len(raw) > bound or (expected is not None and raw != expected):
        raise DisposableRecoveryRejected("disposable recovery input digest or size mismatch")
    return raw


def _unique_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise DisposableRecoveryRejected("duplicate recovery record field")
        result[key] = value
    return result


def _journal(root: Path) -> dict:
    try:
        record = json.loads(
            _regular(root / JOURNAL, MAX_RECORD).decode("utf-8"),
            object_pairs_hook=_unique_pairs,
        )
    except (UnicodeError, ValueError, TypeError) as exc:
        raise DisposableRecoveryRejected("invalid recovery transaction JSON") from exc
    expected = {"version", "state", "label", "candidate_plist_sha256",
                "baseline_plist_sha256"}
    if not isinstance(record, dict) or set(record) != expected:
        raise DisposableRecoveryRejected("recovery transaction schema mismatch")
    label = record["label"]
    if (type(record["version"]) is not int or record["version"] != 1
            or record["state"] not in ("armed", "rollback_started", "rolled_back")
            or not isinstance(label, str)
            or not label.startswith(LABEL_PREFIX)
            or len(label) > 100
            or not label[len(LABEL_PREFIX):].isalnum()
            or not label[len(LABEL_PREFIX):]):
        raise DisposableRecoveryRejected("disposable service label/state invalid")
    for field in ("candidate_plist_sha256", "baseline_plist_sha256"):
        digest = record[field]
        if (not isinstance(digest, str) or len(digest) != 64
                or any(c not in "0123456789abcdef" for c in digest)):
            raise DisposableRecoveryRejected("recovery plist hash invalid")
    return record


def _checked_plist(root: Path, name: str, label: str, digest: str) -> Path:
    path = root / name
    data = _regular(path, MAX_PLIST)
    if hashlib.sha256(data).hexdigest() != digest:
        raise DisposableRecoveryRejected("recovery plist integrity mismatch")
    try:
        doc = plistlib.loads(data)
    except (ValueError, TypeError, OverflowError) as exc:
        raise DisposableRecoveryRejected("invalid disposable launchd plist") from exc
    if not isinstance(doc, dict) or doc.get("Label") != label:
        raise DisposableRecoveryRejected("disposable plist has unexpected service label")
    argv = doc.get("ProgramArguments")
    if (not isinstance(argv, list) or len(argv) != 2 or argv[0] != "/bin/sleep"
            or argv[1] not in ("120", "121") or not doc.get("RunAtLoad")
            or doc.get("KeepAlive") is not True or "Disabled" in doc):
        raise DisposableRecoveryRejected("disposable plist has untrusted service commands")
    if set(doc) != {"Label", "ProgramArguments", "RunAtLoad", "KeepAlive"}:
        raise DisposableRecoveryRejected("disposable plist has untrusted launchd attributes")
    if name == "baseline.plist" and argv[1] != "120":
        raise DisposableRecoveryRejected("untrusted baseline service command")
    if name == "candidate.plist" and argv[1] != "121":
        raise DisposableRecoveryRejected("untrusted candidate service command")
    return path


def _launchctl(*argv: str, expected: tuple[int, ...] = (0,)) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(
            ["/bin/launchctl", *argv], capture_output=True,
            text=True, timeout=15, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise DisposableRecoveryRejected("disposable launchctl command timed out") from exc
    if result.returncode not in expected:
        raise DisposableRecoveryRejected("disposable launchd operation rejected")
    return result


def _generation(result: subprocess.CompletedProcess[str]) -> str:
    """Fail closed unless launchd explicitly identifies our test argv.

    A matching test label alone is not evidence of the source generation.
    launchctl print renders argument tokens as separate indented lines.
    """
    if result.returncode == 113:
        return "absent"
    if result.returncode != 0:
        raise DisposableRecoveryRejected("cannot identify test service")
    text = result.stdout
    if "/bin/sleep" not in text:
        raise DisposableRecoveryRejected("unrecognized disposable command")
    baseline = re.search(r"(?m)^\s*120\s*$", text) is not None
    candidate = re.search(r"(?m)^\s*121\s*$", text) is not None
    if baseline == candidate:
        raise DisposableRecoveryRejected("ambiguous service source generation")
    return "baseline" if baseline else "candidate"


def _persist_state(root: Path, record: dict, state: str) -> dict:
    """Persist a pre-effect recovery journal, fsync and atomic rename."""
    updated = dict(record, state=state)
    temporary = root / ".recovery-journal-next"
    if temporary.exists() or temporary.is_symlink():
        raise DisposableRecoveryRejected("recovery journal temp collision")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL
                 | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(updated, f, sort_keys=True)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(temporary, root / JOURNAL)
        df = os.open(root, os.O_RDONLY)
        try:
            os.fsync(df)
        finally:
            os.close(df)
    finally:
        temporary.unlink(missing_ok=True)
    return updated


def recover(root_path: Path) -> dict[str, object]:
    """Restore a real isolated launchd service with crash-safe retry.

    The only recovery service labels supported are in the private disposable
    namespace. Record rollback_started BEFORE any bootout, so an OS crash
    between bootout/bootstrap can restart recovery without replaying tasks.
    Never replace an unknown live service or delete a queue.
    """
    root = _root(root_path)
    record = _journal(root)
    baseline = _checked_plist(root, "baseline.plist", record["label"],
                              record["baseline_plist_sha256"])
    _checked_plist(root, "candidate.plist", record["label"],
                   record["candidate_plist_sha256"])
    target = f"gui/{os.getuid()}/{record['label']}"
    domain = f"gui/{os.getuid()}"
    seen = _generation(_launchctl("print", target, expected=(0, 113)))
    if record["state"] == "rolled_back":
        if seen != "baseline":
            raise DisposableRecoveryRejected("recorded rollback baseline is not running")
        return {"version": 1, "state": "rolled_back", "idempotent": True,
                "real_launchd_restore": True, "production_service_changed": False,
                "tasks_replayed": False}
    if record["state"] == "armed":
        if seen != "candidate":
            raise DisposableRecoveryRejected("armed recovery has no attested candidate")
        record = _persist_state(root, record, "rollback_started")
    # A prior external watchdog may have crashed before/after bootout or
    # after baseline bootstrap. Never bootout an already restored baseline.
    if seen == "candidate":
        _launchctl("bootout", target)
        seen = _generation(_launchctl("print", target, expected=(0, 113)))
        if seen != "absent":
            raise DisposableRecoveryRejected("candidate remained after bootout")
    if seen == "absent":
        _launchctl("bootstrap", domain, str(baseline))
    final = _generation(_launchctl("print", target, expected=(0, 113)))
    if final != "baseline":
        raise DisposableRecoveryRejected("verified baseline service did not register")
    _persist_state(root, record, "rolled_back")
    return {"version": 1, "state": "rolled_back", "idempotent": False,
            "real_launchd_restore": True, "production_service_changed": False,
            "tasks_replayed": False}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Disposable launchd independent B1c rollback")
    parser.add_argument("root", type=Path)
    args = parser.parse_args(argv)
    print(json.dumps(recover(args.root), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
