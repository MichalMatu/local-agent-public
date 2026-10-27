"""Offline, non-installing launchd candidate configuration for the sealed B1c core.

Only a separate, authorized external operator may later activate a source
switch. This module cannot modify a LaunchAgent, restart a daemon, freeze
queues, bypass the operator pin, or claim live queue quiescence.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import plistlib
import stat
import subprocess
import sys

# Support both -m invocation and an absolute script path from an isolated
# driver checkout, without relying on the ambient PYTHONPATH.
REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from local_agent.platform import macos_launchd  # noqa: E402
from local_agent.supervisor.macos_release_admission import REQUIRED_ENV  # noqa: E402
from scripts import macos_b1c_candidate as candidate  # noqa: E402

_PLIST_LIMIT = 16 * 1024
APPROVED_TESTED_CANDIDATE_SHA = "1df07c5160269f45f774c6d0144ed2a3b971f52e"


class LaunchdCandidateRejected(ValueError):
    """Unsafe or unverified B1c service source or launch policy."""


def _original_plist(path: Path) -> bytes:
    if path.is_symlink():
        raise LaunchdCandidateRejected("original Mac service plist symlink")
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except OSError as exc:
        raise LaunchdCandidateRejected("original Mac plist is unavailable") from exc
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= _PLIST_LIMIT
                or info.st_uid != os.getuid() or info.st_mode & 0o022):
            raise LaunchdCandidateRejected("original Mac plist identity or mode unsafe")
        with os.fdopen(fd, "rb", closefd=False) as handle:
            data = handle.read(_PLIST_LIMIT + 1)
    finally:
        os.close(fd)
    if len(data) > _PLIST_LIMIT:
        raise LaunchdCandidateRejected("original Mac plist exceeds size bound")
    return data


def _verify_accepted_installed_base(root: Path, home: Path) -> None:
    """Read-only proof of the exact installed B2 source behind the plist."""
    try:
        real = root.resolve(strict=True)
        mode = root.lstat()
    except OSError as exc:
        raise LaunchdCandidateRejected("installed B2 source directory unavailable") from exc
    if (root != real or home not in root.parents or not stat.S_ISDIR(mode.st_mode)
            or mode.st_uid != os.getuid() or mode.st_mode & 0o022
            or (root / ".git").is_symlink()):
        raise LaunchdCandidateRejected("installed B2 source path is unsafe")
    try:
        result = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=12, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise LaunchdCandidateRejected("installed B2 revision cannot be verified") from exc
    if result.returncode or result.stdout.strip() != candidate.BASE_SHA:
        raise LaunchdCandidateRejected("installed Mac source is not the accepted B2")


def _validated_original(
    original: bytes, *, installed_root: Path, home: Path,
) -> dict:
    if not isinstance(original, bytes) or not 0 < len(original) <= _PLIST_LIMIT:
        raise LaunchdCandidateRejected("invalid original Mac plist byte bound")
    if not installed_root.is_absolute() or installed_root.is_symlink():
        raise LaunchdCandidateRejected("installed Mac root is not trusted")
    if not home.is_absolute() or home.is_symlink():
        raise LaunchdCandidateRejected("Mac home path is not trusted")
    _verify_accepted_installed_base(installed_root, home)
    try:
        document = plistlib.loads(original)
    except (ValueError, TypeError, OverflowError) as exc:
        raise LaunchdCandidateRejected("original Mac plist malformed") from exc
    if not isinstance(document, dict):
        raise LaunchdCandidateRejected("original Mac plist must be a dictionary")
    argv = document.get("ProgramArguments")
    if (not isinstance(argv, list) or len(argv) != 6
            or any(not isinstance(arg, str) for arg in argv)
            or argv[2] != "--registry" or argv[4] != "--max-workers"):
        raise LaunchdCandidateRejected("original Mac service argv is not parallel B2")
    try:
        worker_count = int(argv[5])
    except ValueError as exc:
        raise LaunchdCandidateRejected("invalid original Mac worker limit") from exc
    if str(worker_count) != argv[5]:
        raise LaunchdCandidateRejected("noncanonical original Mac worker limit")
    registry = home / "Library" / "Application Support" / "local-agent" / "repositories.json"
    try:
        expected = macos_launchd.build_launch_agent(
            "parallel", repo_root=installed_root, home=home,
            max_workers=worker_count, registry_path=registry,
        )
    except ValueError as exc:
        raise LaunchdCandidateRejected("original Mac worker count invalid") from exc
    # Existing Mac B2 is a separate immutable source checkout and uses the
    # stable interpreter from the developer installation. Do not redirect
    # interpreter provenance to a non-existent candidate .venv.
    interpreters = {
        str(installed_root / ".venv" / "bin" / "python"),
        str(home / "local-agent" / ".venv" / "bin" / "python"),
    }
    if argv[0] not in interpreters:
        raise LaunchdCandidateRejected("Mac interpreter differs from trusted B2 paths")
    expected["ProgramArguments"][0] = argv[0]
    if document != expected:
        raise LaunchdCandidateRejected("original Mac service differs from trusted B2 policy")
    return document


def render_disabled_candidate_plan(
    *, sandbox: Path, overlay_sha: str, original_plist: bytes,
    installed_root: Path, home: Path,
) -> bytes:
    """Render an uninstalled, Disabled=True candidate from exact B2 policy.

    RunAtLoad/KeepAlive are inherited for review but Disabled=True prevents
    accidental launch on the user's original com.michal.local-agent label.
    B1c reads source from a *different* verified clean checkout; the
    original installed venv Python and HOME/registry paths are preserved.
    """
    if sys.platform != "darwin":
        raise LaunchdCandidateRejected("offline B1c launchd policy is macOS only")
    try:
        sealed = candidate.verify_sealed(sandbox, overlay_sha)
    except candidate.CandidateRejected as exc:
        raise LaunchdCandidateRejected("B1c source is not exact sealed candidate") from exc
    if sealed["candidate_sha"] != APPROVED_TESTED_CANDIDATE_SHA:
        raise LaunchdCandidateRejected("Mac B1c candidate differs from fully tested release SHA")
    source = (Path(sandbox) / "candidate").resolve(strict=True)
    entrypoint = source / "agent_entrypoint.py"
    if entrypoint.is_symlink() or not entrypoint.is_file():
        raise LaunchdCandidateRejected("sealed B1c entrypoint is missing or redirected")
    original = _validated_original(
        original_plist, installed_root=Path(installed_root), home=Path(home),
    )
    argv = list(original["ProgramArguments"])
    argv[1] = str(source / "agent_entrypoint.py")
    variables = dict(original["EnvironmentVariables"])
    if REQUIRED_ENV in variables:
        raise LaunchdCandidateRejected("original Mac plist unexpectedly has admission policy")
    variables[REQUIRED_ENV] = "1"
    plan = dict(original)
    plan["ProgramArguments"] = argv
    plan["WorkingDirectory"] = str(source)
    plan["EnvironmentVariables"] = variables
    plan["Disabled"] = True
    # Operator approval, source permanence and independent recovery are
    # NOT supplied or implied by sealed Git provenance or this plist.
    if not sealed["clean_git_checkout"]:
        raise LaunchdCandidateRejected("candidate source checkout must be clean")
    return plistlib.dumps(plan, fmt=plistlib.FMT_XML, sort_keys=True)


def inspect_disabled_candidate_plan(
    data: bytes, *, sandbox: Path, overlay_sha: str,
    original_plist: bytes, installed_root: Path, home: Path,
) -> dict[str, object]:
    """Require byte-for-byte canonical plan and never assert release readiness."""
    expected = render_disabled_candidate_plan(
        sandbox=sandbox, overlay_sha=overlay_sha, original_plist=original_plist,
        installed_root=installed_root, home=home,
    )
    if type(data) is not bytes or data != expected:
        raise LaunchdCandidateRejected("Mac B1c plan modified or not canonical")
    return {
        "version": 1,
        "exact_sealed_candidate_verified": True,
        "required_admission_in_launchd": True,
        "original_service_policy_preserved": True,
        "disabled": True,
        "installed": False,
        "activated": False,
        "live_workers_drained": False,
        "independent_recovery_installed": False,
        "boot_or_login_rehearsed": False,
        "rollback_authority": False,
        "source_switch_authorized": False,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Read-only disabled Mac B1c plist check")
    parser.add_argument("--sandbox", type=Path, required=True)
    parser.add_argument("--overlay-sha", required=True)
    parser.add_argument("--original-plist", type=Path, required=True)
    parser.add_argument("--installed-root", type=Path, required=True)
    parser.add_argument("--home", type=Path, required=True)
    args = parser.parse_args(argv)
    original = _original_plist(args.original_plist)
    proposed = render_disabled_candidate_plan(
        sandbox=args.sandbox, overlay_sha=args.overlay_sha,
        original_plist=original, installed_root=args.installed_root, home=args.home,
    )
    proof = inspect_disabled_candidate_plan(
        proposed, sandbox=args.sandbox, overlay_sha=args.overlay_sha,
        original_plist=original, installed_root=args.installed_root, home=args.home,
    )
    print(json.dumps(proof, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
