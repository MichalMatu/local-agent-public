"""Explicit local-admin repair of the *specific* unsafe macOS sudo helper parent.

Never invoked by the Local Agent worker, remotely or automatically. This
modifies a shared /usr/local/sbin directory ONLY after explicit admin review.
The original ownership/mode are journaled for supervised disaster recovery;
restoring user write access while NOPASSWD is active is deliberately unsupported.
"""
from __future__ import annotations

import json
import os
import pwd
import stat
from pathlib import Path
from typing import Any

from local_agent.supervisor.host_owner_admin import (
    DEFAULT_RECORD, _FILE_FLAGS, _ROOT_DIR_FLAGS,
    _protected_directory, _require_local_admin,
)
from local_agent.supervisor.host_pairing_boundary import (
    _require_no_macos_acl, _require_root_only,
)
from local_agent.supervisor.host_mvp_readiness import _HELPER

_TARGET = Path("/usr/local/sbin")
_JOURNAL = "sudo-helper-parent-repair.json"


def _inspect_target() -> tuple[int, os.stat_result, os.stat_result]:
    opened: list[int] = []
    try:
        fd = os.open("/", _ROOT_DIR_FLAGS)
        opened.append(fd)
        parent = Path("/")
        _require_root_only(os.fstat(fd), directory=True)
        _require_no_macos_acl(parent)
        for component in ("usr", "local"):
            nxt = os.open(component, _ROOT_DIR_FLAGS, dir_fd=opened[-1])
            opened.append(nxt)
            parent /= component
            _require_root_only(os.fstat(nxt), directory=True)
            _require_no_macos_acl(parent)
        targetfd = os.open("sbin", _ROOT_DIR_FLAGS, dir_fd=opened[-1])
        opened.append(targetfd)
        before = os.fstat(targetfd)
        if not stat.S_ISDIR(before.st_mode):
            raise ValueError("sudo helper parent is not a directory")
        _require_no_macos_acl(_TARGET)
        helperfd = os.open(_HELPER.name, _FILE_FLAGS, dir_fd=targetfd)
        opened.append(helperfd)
        helper = os.fstat(helperfd)
        _require_root_only(helper, directory=False)
        _require_no_macos_acl(_HELPER)
        # Preserve the checked directory descriptor for a path-safe repair.
        return os.dup(targetfd), before, helper
    finally:
        for item in reversed(opened):
            os.close(item)



def _require_service_uid_not_wheel(uid: int) -> None:
    """A root:wheel 0775 transition is unsafe if the service UID is in wheel."""
    try:
        account = pwd.getpwuid(uid)
        groups = os.getgrouplist(account.pw_name, account.pw_gid)
    except (KeyError, OSError) as exc:
        raise ValueError("cannot verify service account wheel membership") from exc
    if 0 in groups:
        raise ValueError("service account is in wheel; no safe ownership transition")

def protect_sudo_helper_parent(
    *, expected_uid: int, expected_mode: int, confirmation: str,
    reviewed_shared_directory: bool,
    journal_record: Path = DEFAULT_RECORD,
) -> dict[str, Any]:
    """Root-only, backup-first one-way remediation; never changes sudoers.

    Offline administrative change requires the agent's service to be drained
    independently; this function cannot prove no malicious UID process exists.
    """
    _require_local_admin()
    if not reviewed_shared_directory:
        raise ValueError("administrator must review shared /usr/local/sbin dependencies")
    if (type(expected_uid) is not int or type(expected_mode) is not int
            or expected_uid <= 0 or expected_mode != 0o775):
        raise ValueError("explicit original owner/mode not accepted")
    _require_service_uid_not_wheel(expected_uid)
    expected = f"PROTECT /usr/local/sbin {expected_uid} 0775"
    if confirmation != expected:
        raise ValueError("exact local repair confirmation required")
    fd, before, helper = _inspect_target()
    try:
        if (before.st_uid != expected_uid
                or stat.S_IMODE(before.st_mode) != expected_mode):
            raise ValueError("shared helper parent changed since administrator review")
        with _protected_directory(journal_record, create=True) as rootfd:
            evidence = {
                "version": 1, "target": str(_TARGET),
                "original_uid": before.st_uid, "original_gid": before.st_gid,
                "original_mode": stat.S_IMODE(before.st_mode),
                "target_mode": 0o755, "target_uid": 0, "target_gid": 0,
                "helper_device": helper.st_dev, "helper_inode": helper.st_ino,
                "status": "prepared_before_change",
            }
            data = (json.dumps(evidence, sort_keys=True) + "\n").encode("utf-8")
            # No journal overwrite or hidden retry of an uncertain prior change.
            journal = os.open(
                _JOURNAL, os.O_WRONLY | os.O_CREAT | os.O_EXCL
                | getattr(os, "O_NOFOLLOW", 0), 0o600, dir_fd=rootfd,
            )
            with os.fdopen(journal, "wb") as out:
                out.write(data)
                out.flush()
                os.fsync(out.fileno())
            os.fsync(rootfd)

            # Transfer ownership FIRST. chmod(0555) would not protect
            # against the old UID owner, who can chmod its own directory back.
            # During the root:wheel 0775 interval, the service UID must
            # not be a member of wheel (validated above). Finish at 0755.
            # An interrupted ownership change requires manual review.
            os.fchown(fd, 0, 0)
            os.fchmod(fd, 0o755)
            _require_root_only(os.fstat(fd), directory=True)
            _require_no_macos_acl(_TARGET)
            currentfd = os.open(_HELPER.name, _FILE_FLAGS, dir_fd=fd)
            try:
                current = os.fstat(currentfd)
                _require_root_only(current, directory=False)
                if (current.st_dev, current.st_ino) != (helper.st_dev, helper.st_ino):
                    raise ValueError("privileged helper changed during repair; inspect manually")
                _require_no_macos_acl(_HELPER)
            finally:
                os.close(currentfd)
            os.fsync(fd)
            return {
                "status": "helper_parent_protected",
                "target": str(_TARGET),
                "owner_uid": 0, "mode": "0755",
                "journal": str(journal_record.parent / _JOURNAL),
                "sudo_scope_verified": False, "standing_execution_enabled": False,
                "execution_authority": "not_asserted",
            }
    finally:
        os.close(fd)
