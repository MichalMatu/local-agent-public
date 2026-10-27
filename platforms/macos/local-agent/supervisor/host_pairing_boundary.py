"""Read-only macOS root-owned owner-choice boundary inspection.

This does not enroll an owner or authorize any execution. A service-user
writable pairing.json, a TTY prompt and a Git remote are not owner authority.
"""
from __future__ import annotations

import os
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any

from local_agent.repository.host_facts import load_local_host_identity
from local_agent.supervisor.host_local_pairing import _MAX_RECORD_BYTES, _record_from_bytes
from local_agent.supervisor.host_pairing_plan import _canonical_repository


def _require_root_only(info: os.stat_result, *, directory: bool) -> None:
    correct_type = stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)
    if (not correct_type or info.st_uid != 0 or info.st_mode & 0o022
            or (not directory and info.st_nlink != 1)):
        raise ValueError("root-only macOS owner state is not protected from service-user writes")


def _require_no_macos_acl(path: Path) -> None:
    """Deny any ACL or unavailable ACL inspection; POSIX mode alone is insufficient."""
    check = subprocess.run(
        ["/bin/ls", "-lde", str(path)], capture_output=True, text=True,
        timeout=5, check=False,
    )
    if check.returncode or check.stderr or len(check.stdout.splitlines()) != 1:
        raise ValueError("cannot independently inspect macOS file ACL")
    mode = check.stdout.split(maxsplit=1)[0]
    if len(mode) not in (10, 11) or (len(mode) == 11 and mode[10] != "@"):
        raise ValueError("macOS ACL or unknown file mode is not trusted")


def inspect_root_owned_pairing(
    *, record_path: Path, identity_file: Path, expected_repository: str,
) -> dict[str, Any]:
    """Inspect a separate, already-existing root-owned *copy* of the ledger.

    Never installs/creates it; never imports into the worker. The OS boundary
    excludes service-user writes, not a malicious root/admin or sudo authority.
    """
    if sys.platform != "darwin":
        raise ValueError("root-owned pairing boundary inspection is macOS-only")
    path = Path(record_path).expanduser()
    if not path.is_absolute() or path == Path("/") or path.name != "owner-pairing.json":
        raise ValueError("expected an absolute dedicated owner-pairing.json")
    repo = _canonical_repository(expected_repository)
    parts = path.parts[1:]
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    root_fd = os.open("/", flags | getattr(os, "O_DIRECTORY", 0))
    opened = [root_fd]
    try:
        _require_root_only(os.fstat(root_fd), directory=True)
        _require_no_macos_acl(Path("/"))
        parent = Path("/")
        for component in parts[:-1]:
            fd = os.open(component, flags | getattr(os, "O_DIRECTORY", 0),
                         dir_fd=opened[-1])
            opened.append(fd)
            parent = parent / component
            _require_root_only(os.fstat(fd), directory=True)
            _require_no_macos_acl(parent)
        fd = os.open(parts[-1], flags, dir_fd=opened[-1])
        opened.append(fd)
        info = os.fstat(fd)
        _require_root_only(info, directory=False)
        _require_no_macos_acl(path)
        if not 0 < info.st_size <= _MAX_RECORD_BYTES:
            raise ValueError("root-owned pairing record has invalid size")
        with os.fdopen(os.dup(fd), "rb") as stream:
            record = _record_from_bytes(stream.read(_MAX_RECORD_BYTES + 1))
        # A root-only directory cannot be replaced by the service user.
        # Validate the same identity/selection without accepting task metadata.
        identity = load_local_host_identity(identity_file)
        if (record["repository"] != repo or record["host_id"] != identity.host_id
                or record["installation_id"] != identity.installation_id):
            raise ValueError("root-owned pairing record does not match local installation")
        return {
            "status": "root_owned_owner_choice_observed",
            "paired": record["state"] == "paired",
            "repository": repo, "host_id": identity.host_id,
            "installation_id": identity.installation_id, "epoch": record["epoch"],
            "macos_root_write_boundary": "observed",
            "github_writers_verified": False,
            "standing_execution_enabled": False,
            "execution_authority": "not_asserted",
        }
    finally:
        for descriptor in reversed(opened):
            os.close(descriptor)
