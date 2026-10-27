"""Stage a narrow Mac B1c engine from the installed B2 Git revision.

Operates ONLY on a marked, owner-private disposable Git checkout under the
macOS temp directory; does not install, launch, freeze or update Local Agent.
The pinned overlay modules, including admission, are source code, not a
release-authorized service configuration. Keep the running baseline intact.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile

BASE_SHA = "cccc67fce71db02d0e878172b1e96ce858ee96ea"
MARKER = ".mac-b1c-disposable"
MARKER_BYTES = b"local-agent-mac-b1c-staging-only-v1\n"
MAX_BLOB_BYTES = 2 * 1024 * 1024
MODIFIED = (
    "local_agent/daemon/service.py",
    "local_agent/development/operator_campaign.py",
    "local_agent/repository/worker.py",
    "local_agent/supervisor/conversation.py",
    "local_agent/supervisor/host_remote_worker.py",
    "local_agent/supervisor/host_tasks.py",
    "local_agent/supervisor/orchestrator.py",
    "local_agent/supervisor/worker.py",
)
ADDED = (
    "local_agent/supervisor/macos_release_admission.py",
    "tests/test_macos_release_admission.py",
    "tests/test_macos_release_admission_coverage.py",
)
PATHS = tuple(sorted((*MODIFIED, *ADDED)))


class CandidateRejected(ValueError):
    """Refuse ambiguous candidate inputs rather than touching installed code."""


def _git(checkout: Path, *args: str, env: dict[str, str] | None = None) -> bytes:
    result = subprocess.run(
        ["git", "-C", str(checkout), *args], capture_output=True,
        check=False, timeout=25, env=env,
    )
    if result.returncode:
        raise CandidateRejected("candidate Git verification failed")
    return result.stdout


def _private_dir(path: Path) -> None:
    if path.is_symlink():
        raise CandidateRejected("candidate staging directory symlink")
    try:
        mode = path.lstat()
    except OSError as exc:
        raise CandidateRejected("candidate staging directory missing") from exc
    if not stat.S_ISDIR(mode.st_mode) or mode.st_uid != os.getuid() or mode.st_mode & 0o077:
        raise CandidateRejected("candidate staging directory must be owner-private")


def _disposable(root: Path) -> Path:
    if sys.platform != "darwin":
        raise CandidateRejected("candidate staging requires macOS")
    raw = Path(root)
    if not raw.is_absolute() or raw.is_symlink():
        raise CandidateRejected("candidate staging requires absolute, nonlinked root")
    try:
        canonical = raw.resolve(strict=True)
    except OSError as exc:
        raise CandidateRejected("candidate root missing") from exc
    allowed = (
        Path("/private/tmp").resolve(strict=True),
        Path(tempfile.gettempdir()).resolve(strict=True),
    )
    if (not canonical.name.startswith("la-rollout-b1c-")
            or all(parent not in canonical.parents for parent in allowed)
            or canonical != raw):
        raise CandidateRejected("candidate must be within a dedicated macOS temp root")
    _private_dir(canonical)
    marker = canonical / MARKER
    if marker.is_symlink():
        raise CandidateRejected("candidate marker symlink")
    try:
        details = marker.lstat()
        if (not stat.S_ISREG(details.st_mode) or details.st_uid != os.getuid()
                or details.st_mode & 0o077 or details.st_nlink != 1
                or marker.read_bytes() != MARKER_BYTES):
            raise CandidateRejected("invalid disposable candidate marker")
    except OSError as exc:
        raise CandidateRejected("missing disposable candidate marker") from exc
    checkout = canonical / "candidate"
    _private_dir(checkout)
    if checkout.resolve(strict=True) != checkout:
        raise CandidateRejected("redirected candidate checkout")
    if not (checkout / ".git").is_dir() or (checkout / ".git").is_symlink():
        raise CandidateRejected("candidate must have a real Git directory")
    return checkout


def _sha(value: str) -> str:
    if not isinstance(value, str) or len(value) != 40 or any(
        c not in "0123456789abcdef" for c in value
    ):
        raise CandidateRejected("candidate overlay revision must be an exact SHA-1")
    return value


def _manifest(checkout: Path, source_sha: str) -> dict[str, str]:
    manifest = {}
    for path in PATHS:
        data = _git(checkout, "show", f"{source_sha}:{path}")
        if not data or len(data) > MAX_BLOB_BYTES:
            raise CandidateRejected("missing or oversized candidate module")
        manifest[path] = hashlib.sha256(data).hexdigest()
    return manifest


def _status(checkout: Path) -> tuple[set[str], set[str]]:
    changed = set(filter(None, _git(checkout, "diff", "--name-only", "HEAD", "--").decode().splitlines()))
    untracked = set(filter(None, _git(
        checkout, "ls-files", "--others", "--exclude-standard", "-z",
    ).decode().split("\0")))
    return changed, untracked


def verify(root: Path, source_sha: str) -> dict[str, object]:
    checkout = _disposable(root)
    source_sha = _sha(source_sha)
    if _git(checkout, "rev-parse", "HEAD").decode().strip() != BASE_SHA:
        raise CandidateRejected("candidate checkout is not the installed B2 baseline")
    if _git(checkout, "merge-base", "--is-ancestor", BASE_SHA, source_sha):
        raise CandidateRejected("candidate source ancestry unavailable")
    expected = _manifest(checkout, source_sha)
    changed, untracked = _status(checkout)
    if changed != set(MODIFIED) or untracked != set(ADDED):
        raise CandidateRejected("candidate overlay has unexpected changed paths")
    for path, digest in expected.items():
        target = checkout / path
        if target.is_symlink() or any(
            (checkout.joinpath(*Path(path).parts[:count])).is_symlink()
            for count in range(1, len(Path(path).parts))
        ):
            raise CandidateRejected("candidate code symlink")
        try:
            details = target.lstat()
            if not stat.S_ISREG(details.st_mode) or details.st_nlink != 1:
                raise CandidateRejected("candidate file is not a regular private checkout file")
            data = target.read_bytes()
        except OSError as exc:
            raise CandidateRejected("candidate file unavailable") from exc
        if hashlib.sha256(data).hexdigest() != digest:
            raise CandidateRejected("candidate module digest mismatch")
    return {
        "version": 1,
        "base_sha": BASE_SHA,
        "overlay_sha": source_sha,
        "files": expected,
        "changed_source_files": len(MODIFIED),
        "added_source_files": len(ADDED),
        "disposable_only": True,
        "installed_service_changed": False,
        "source_switch_authorized": False,
        "engine_started": False,
    }


def stage(root: Path, source_sha: str) -> dict[str, object]:
    checkout = _disposable(root)
    source_sha = _sha(source_sha)
    if _git(checkout, "rev-parse", "HEAD").decode().strip() != BASE_SHA:
        raise CandidateRejected("candidate checkout is not the installed B2 baseline")
    if _git(checkout, "status", "--porcelain", "-z"):
        raise CandidateRejected("disposable baseline checkout is not clean")
    _git(checkout, "merge-base", "--is-ancestor", BASE_SHA, source_sha)
    _manifest(checkout, source_sha)  # Fail before editing if any pinned source is missing.
    _git(checkout, "restore", "--source", source_sha, "--worktree", "--", *PATHS)
    return verify(root, source_sha)


def verify_sealed(root: Path, source_sha: str) -> dict[str, object]:
    """Prove a clean, exact standalone candidate commit parented on B2."""
    checkout = _disposable(root)
    source_sha = _sha(source_sha)
    candidate_sha = _git(checkout, "rev-parse", "HEAD").decode().strip()
    if candidate_sha in (BASE_SHA, source_sha):
        raise CandidateRejected("candidate is not an independently sealed B2 overlay")
    if _git(checkout, "rev-parse", "HEAD^").decode().strip() != BASE_SHA:
        raise CandidateRejected("candidate commit has unexpected parent")
    if _git(checkout, "status", "--porcelain", "-z"):
        raise CandidateRejected("sealed candidate checkout is dirty")
    changed = set(filter(None, _git(
        checkout, "diff-tree", "--no-commit-id", "--name-only", "-r", "HEAD",
    ).decode().splitlines()))
    if changed != set(PATHS):
        raise CandidateRejected("sealed candidate commit contains unlisted files")
    manifest = _manifest(checkout, source_sha)
    for name, expected in manifest.items():
        path = checkout / name
        if path.is_symlink() or any(
            (checkout.joinpath(*Path(name).parts[:count])).is_symlink()
            for count in range(1, len(Path(name).parts))
        ):
            raise CandidateRejected("sealed candidate source symlink")
        if hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            raise CandidateRejected("sealed candidate source digest mismatch")
    return {
        "version": 1,
        "base_sha": BASE_SHA,
        "overlay_sha": source_sha,
        "candidate_sha": candidate_sha,
        "files": manifest,
        "clean_git_checkout": True,
        "disposable_only": True,
        "installed_service_changed": False,
        "source_switch_authorized": False,
        "engine_started": False,
    }


def seal(root: Path, source_sha: str) -> dict[str, object]:
    """Make an immutable identified code revision only in disposable Git.

    Do not put a dirty B2 worktree into a LaunchAgent: the reported revision
    would still be B2, hiding the exact source now running.
    """
    verify(root, source_sha)
    checkout = _disposable(root)
    source_sha = _sha(source_sha)
    _git(checkout, "add", "--", *PATHS)
    env = dict(
        os.environ,
        GIT_AUTHOR_DATE="2000-01-01T00:00:00 +0000",
        GIT_COMMITTER_DATE="2000-01-01T00:00:00 +0000",
        GIT_AUTHOR_NAME="Local Agent Mac B1c",
        GIT_COMMITTER_NAME="Local Agent Mac B1c",
        GIT_AUTHOR_EMAIL="local-agent-b1c@example.invalid",
        GIT_COMMITTER_EMAIL="local-agent-b1c@example.invalid",
    )
    _git(
        checkout, "-c", "core.hooksPath=/dev/null", "-c", "commit.gpgsign=false",
        "commit", "--quiet", "--no-gpg-sign", "--no-verify",
        "-m", f"Mac B1c minimal engine overlay {source_sha}", env=env,
    )
    return verify_sealed(root, source_sha)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Isolated Mac B1c source candidate")
    choices = parser.add_mutually_exclusive_group(required=True)
    choices.add_argument("--stage", action="store_true")
    choices.add_argument("--verify", action="store_true")
    choices.add_argument("--seal", action="store_true")
    choices.add_argument("--verify-sealed", action="store_true")
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--overlay-sha", required=True)
    args = parser.parse_args(argv)
    if args.stage:
        report = stage(args.root, args.overlay_sha)
    elif args.seal:
        report = seal(args.root, args.overlay_sha)
    elif args.verify_sealed:
        report = verify_sealed(args.root, args.overlay_sha)
    else:
        report = verify(args.root, args.overlay_sha)
    print(json.dumps(report, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
