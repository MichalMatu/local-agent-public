"""Read-only macOS launchd policy for the persistent, exact tested B1c checkout.

Unlike the disposable staging rehearsal, this checks the private B1c code
already staged outside /tmp. It never writes a plist, touches launchctl,
reconciles a claim, thaws admission or grants approval to deploy.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import plistlib
import stat
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from local_agent.supervisor.macos_release_admission import REQUIRED_ENV  # noqa: E402
from scripts import macos_b1c_candidate as candidate  # noqa: E402
from scripts import macos_b1c_launchd_plan as installed  # noqa: E402

RELEASE_DIR = "local-agent-b1c-release-20261016"
SOURCE_DIR = "source-1df07c51"
EXPECTED_SHA = installed.APPROVED_TESTED_CANDIDATE_SHA


class StagedLaunchdRejected(ValueError):
    """Refuse an ambiguous installed-source or candidate release plan."""


def _owner_private_dir(path: Path) -> None:
    if path.is_symlink():
        raise StagedLaunchdRejected("persistent candidate source symlink")
    try:
        info = path.lstat()
    except OSError as exc:
        raise StagedLaunchdRejected("persistent candidate source missing") from exc
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
            or info.st_mode & 0o077):
        raise StagedLaunchdRejected("persistent candidate directory must be owner-private")


def _git(path: Path, *args: str) -> bytes:
    try:
        proc = subprocess.run(
            ["git", "-C", str(path), *args],
            capture_output=True, timeout=18, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise StagedLaunchdRejected("persistent candidate Git source unavailable") from exc
    if proc.returncode:
        raise StagedLaunchdRejected("persistent candidate Git source invalid")
    return proc.stdout


def verify_staged_source(*, home: Path) -> Path:
    """Re-verify actual clean commit, parent, tree diff and checked-out blobs."""
    if sys.platform != "darwin":
        raise StagedLaunchdRejected("macOS-only candidate source check")
    home = Path(home)
    if not home.is_absolute() or home.is_symlink():
        raise StagedLaunchdRejected("home is not an absolute trusted Mac path")
    release = home / RELEASE_DIR
    source = release / SOURCE_DIR
    _owner_private_dir(release)
    _owner_private_dir(source)
    if (release.resolve(strict=True) != release
            or source.resolve(strict=True) != source):
        raise StagedLaunchdRejected("redirected persistent Mac release source")
    git_dir = source / ".git"
    _owner_private_dir(git_dir)
    # The first live B1c staging incident had a correct Git HEAD but NO origin.
    # Without origin, remote operator-control polling failed repeatedly.
    # A code-only SHA is therefore insufficient for an installable checkout.
    git_origin = _git(source, "remote", "get-url", "origin").decode().strip()
    allowed = {
        f"https://github.com/michalmatu/local-agent-public{suffix}"
        for suffix in ("", ".git")
    }
    allowed.update(
        f"git@github.com:michalmatu/local-agent-public{suffix}"
        for suffix in ("", ".git")
    )
    allowed.update(
        f"ssh://git@github.com/michalmatu/local-agent-public{suffix}"
        for suffix in ("", ".git")
    )
    if git_origin.casefold() not in allowed:
        raise StagedLaunchdRejected("persistent candidate origin is absent or untrusted")
    if _git(source, "rev-parse", "HEAD").decode().strip() != EXPECTED_SHA:
        raise StagedLaunchdRejected("persistent source differs from full-tested B1c commit")
    if _git(source, "rev-parse", "HEAD^").decode().strip() != candidate.BASE_SHA:
        raise StagedLaunchdRejected("persistent source has wrong B2 parent")
    if _git(source, "status", "--porcelain", "-z"):
        raise StagedLaunchdRejected("persistent B1c source has uncommitted changes")
    diff = set(filter(None, _git(
        source, "diff-tree", "--no-commit-id", "--name-only", "-r", "HEAD",
    ).decode().splitlines()))
    if diff != set(candidate.PATHS):
        raise StagedLaunchdRejected("persistent source contains unapproved file changes")
    for name in candidate.PATHS:
        path = source / name
        if (path.is_symlink() or not path.is_file()
                or not stat.S_ISREG(path.stat().st_mode)):
            raise StagedLaunchdRejected("persistent B1c changed file is unsafe")
        blob = _git(source, "show", f"HEAD:{name}")
        if hashlib.sha256(path.read_bytes()).digest() != hashlib.sha256(blob).digest():
            raise StagedLaunchdRejected("persistent candidate source bytes do not match commit")
    entrypoint = source / "agent_entrypoint.py"
    if entrypoint.is_symlink() or not entrypoint.is_file():
        raise StagedLaunchdRejected("persistent candidate entrypoint unavailable")
    return source


def render_disabled_staged_plan(
    *, home: Path, installed_root: Path, original_plist: bytes,
) -> bytes:
    """Retain actual B2 plist semantics while pinning persistent B1c source."""
    source = verify_staged_source(home=home)
    try:
        original = installed._validated_original(
            original_plist, installed_root=Path(installed_root), home=Path(home),
        )
    except installed.LaunchdCandidateRejected as exc:
        raise StagedLaunchdRejected("accepted B2 launch policy not verified") from exc
    argv = list(original["ProgramArguments"])
    argv[1] = str(source / "agent_entrypoint.py")
    env = dict(original["EnvironmentVariables"])
    if REQUIRED_ENV in env:
        raise StagedLaunchdRejected("unexpected inherited B2 required-admission override")
    env[REQUIRED_ENV] = "1"
    planned = dict(original)
    planned["ProgramArguments"] = argv
    planned["WorkingDirectory"] = str(source)
    planned["EnvironmentVariables"] = env
    planned["Disabled"] = True
    return plistlib.dumps(planned, fmt=plistlib.FMT_XML, sort_keys=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Read-only persistent Mac B1c plist proof")
    parser.add_argument("--home", type=Path, required=True)
    parser.add_argument("--installed-root", type=Path, required=True)
    parser.add_argument("--original-plist", type=Path, required=True)
    args = parser.parse_args(argv)
    original = installed._original_plist(args.original_plist)
    plist = render_disabled_staged_plan(
        home=args.home, installed_root=args.installed_root, original_plist=original,
    )
    doc = plistlib.loads(plist)
    print(json.dumps({
        "version": 1,
        "source_sha": EXPECTED_SHA,
        "source_verified": True,
        "source_persistent": True,
        "candidate_plist_sha256": hashlib.sha256(plist).hexdigest(),
        "disabled": doc["Disabled"],
        "required_admission": doc["EnvironmentVariables"][REQUIRED_ENV] == "1",
        "live_service_changed": False,
        "source_switch_authorized": False,
        "independent_recovery_installed": False,
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
