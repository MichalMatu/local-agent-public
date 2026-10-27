"""Root-controlled macOS pairing-choice lifecycle; not worker admission.

The administrator must launch this locally after fixing every service-user
root-escalation route. Root alone is NOT proof of physical presence or
permission to enable general host command intake. Never use this from tasks.
"""
from __future__ import annotations

import fcntl
import os
import secrets
import stat
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from local_agent.repository.host_facts import load_local_host_identity
from local_agent.supervisor.host_local_pairing import (
    PAIRING_RECORD, _MAX_RECORD_BYTES, _record_from_bytes,
    pairing_confirmation, unpair_confirmation,
)
from local_agent.supervisor.host_pairing_boundary import (
    _require_no_macos_acl, _require_root_only,
)
from local_agent.supervisor.host_pairing_plan import _canonical_repository

DEFAULT_RECORD = Path("/Library/Application Support/LocalAgent/owner-pairing.json")
_ROOT_DIR_FLAGS = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
_FILE_FLAGS = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)


def _require_trusted_admin_runtime() -> None:
    """Require isolated Python and root-owned, symlink-free administrative code.

    Never elevate source imported from the project/agent's writable checkout.
    A root-owned CLI tree must be installed by an independent administrator.
    """
    if not sys.flags.isolated:
        raise ValueError("isolated trusted Python runtime required")
    from local_agent.cli import host_remote as cli_module

    candidates = {Path(__file__), Path(cli_module.__file__), Path(sys.executable)}
    # Verify every Local Agent module already imported by the CLI, not only
    # the final entrypoint. A writable dependency can run before the check.
    for name, module in list(sys.modules.items()):
        if name == "local_agent" or name.startswith("local_agent."):
            location = getattr(module, "__file__", None)
            if location:
                candidates.add(Path(location))
    for candidate in sorted(candidates):
        if not candidate.is_absolute():
            raise ValueError("administrative executable/source path is not absolute")
        fd = os.open("/", _ROOT_DIR_FLAGS)
        opened = [fd]
        prefix = Path("/")
        try:
            _require_root_only(os.fstat(fd), directory=True)
            _require_no_macos_acl(prefix)
            for index, part in enumerate(candidate.parts[1:]):
                directory = index < len(candidate.parts[1:]) - 1
                flags = _ROOT_DIR_FLAGS if directory else _FILE_FLAGS
                nxt = os.open(part, flags, dir_fd=opened[-1])
                opened.append(nxt)
                prefix = prefix / part
                _require_root_only(os.fstat(nxt), directory=directory)
                _require_no_macos_acl(prefix)
        finally:
            for item in reversed(opened):
                os.close(item)


def _require_local_admin() -> None:
    if sys.platform != "darwin" or os.geteuid() != 0:
        raise ValueError("macOS local root administrator required")
    # Reject obvious remote/noninteractive contexts, without claiming that
    # environment variables or an interactive TTY prove physical presence.
    if (not sys.stdin.isatty() or not sys.stdout.isatty()
            or any(os.environ.get(x) for x in ("SSH_CONNECTION", "SSH_TTY"))):
        raise ValueError("administrator action requires a local interactive terminal")
    _require_trusted_admin_runtime()


@contextmanager
def _protected_directory(record_path: Path, *, create: bool) -> Iterator[int]:
    path = Path(record_path)
    if not path.is_absolute() or path.name != PAIRING_RECORD or path == Path("/"):
        raise ValueError("expected an absolute protected owner-pairing.json path")
    opened: list[int] = []
    try:
        current = os.open("/", _ROOT_DIR_FLAGS)
        opened.append(current)
        _require_root_only(os.fstat(current), directory=True)
        _require_no_macos_acl(Path("/"))
        parts = path.parts[1:-1]
        parent = Path("/")
        for index, component in enumerate(parts):
            child_path = parent / component
            try:
                child = os.open(component, _ROOT_DIR_FLAGS, dir_fd=current)
            except FileNotFoundError:
                if not create or index != len(parts) - 1:
                    raise
                # Create only the final, dedicated directory. Never create or
                # change any existing shared ancestor or symlink.
                os.mkdir(component, mode=0o755, dir_fd=current)
                child = os.open(component, _ROOT_DIR_FLAGS, dir_fd=current)
                # Set explicit traversal bits regardless of administrator umask,
                # so the non-root Local Agent can inspect this non-secret state.
                os.fchmod(child, 0o755)
            opened.append(child)
            current = child
            parent = child_path
            _require_root_only(os.fstat(current), directory=True)
            _require_no_macos_acl(parent)
        yield current
    finally:
        for fd in reversed(opened):
            os.close(fd)


@contextmanager
def _admin_record_lock(dirfd: int, parent_path: Path) -> Iterator[None]:
    """Serialize pair/revoke epochs without trusting the service-owned ledger."""
    lock_name = ".owner-admin.lock"
    flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(lock_name, flags, 0o600, dir_fd=dirfd)
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode)
                or info.st_mode & 0o077 or info.st_nlink != 1):
            raise ValueError("protected admin lock must be a private regular file")
        _require_root_only(info, directory=False)
        _require_no_macos_acl(parent_path / lock_name)
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def _read_protected(dirfd: int, record_path: Path) -> dict[str, Any] | None:
    try:
        fd = os.open(PAIRING_RECORD, _FILE_FLAGS, dir_fd=dirfd)
    except FileNotFoundError:
        return None
    try:
        info = os.fstat(fd)
        _require_root_only(info, directory=False)
        _require_no_macos_acl(record_path)
        if not 0 < info.st_size <= _MAX_RECORD_BYTES:
            raise ValueError("protected pairing file size invalid")
        with os.fdopen(os.dup(fd), "rb") as src:
            return _record_from_bytes(src.read(_MAX_RECORD_BYTES + 1))
    finally:
        os.close(fd)


def _atomic_protected_write(dirfd: int, record: dict[str, Any]) -> None:
    import json

    data = (json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
    _record_from_bytes(data)
    temp = ".owner-pairing-" + secrets.token_hex(16) + ".tmp"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(temp, flags, 0o600, dir_fd=dirfd)
    try:
        with os.fdopen(fd, "wb") as dst:
            os.fchmod(dst.fileno(), 0o644)  # readable by unprivileged service
            dst.write(data)
            dst.flush()
            os.fsync(dst.fileno())
        os.replace(temp, PAIRING_RECORD, src_dir_fd=dirfd, dst_dir_fd=dirfd)
        os.fsync(dirfd)
    finally:
        try:
            os.unlink(temp, dir_fd=dirfd)
        except FileNotFoundError:
            pass


def _require_privileged_helper_path_protected() -> None:
    """An unsafe existing NOPASSWD helper parent blocks *new* pairing."""
    from local_agent.supervisor.host_mvp_readiness import _HELPER

    info = _HELPER.lstat()
    _require_root_only(info, directory=False)
    _require_no_macos_acl(_HELPER)
    for parent in reversed(_HELPER.parents):
        _require_root_only(parent.lstat(), directory=True)
        _require_no_macos_acl(parent)


def change_protected_owner_choice(
    *, action: str, record_path: Path, identity_file: Path,
    repository: str, expected_host_id: str,
    expected_installation_id: str, expected_control_sha: str | None,
    typed_confirmation: str,
) -> dict[str, Any]:
    """Explicit root-only snapshot pair/revoke; no sudo, Git or worker effects.

    A Git sender cannot invoke this as the service user. It never stores an
    active standing-execution grant, including for a successful pair.
    """
    _require_local_admin()
    if action not in {"pair", "unpair"}:
        raise ValueError("invalid protected owner choice action")
    if action == "pair":
        _require_privileged_helper_path_protected()
    repo = _canonical_repository(repository)
    identity = load_local_host_identity(identity_file)
    if (identity.host_id != expected_host_id
            or identity.installation_id != expected_installation_id):
        raise ValueError("local installation identity mismatch")
    prefix = (pairing_confirmation(repo, identity.host_id) if action == "pair"
              else unpair_confirmation(repo, identity.host_id))
    confirmation = f"{prefix} {identity.installation_id}"
    if typed_confirmation != confirmation:
        raise ValueError("exact owner confirmation required")
    if (action == "pair"
            and (not isinstance(expected_control_sha, str)
                 or len(expected_control_sha) != 40
                 or any(c not in "0123456789abcdef" for c in expected_control_sha))):
        raise ValueError("exact reviewed remote control commit required")
    with (
        _protected_directory(record_path, create=action == "pair") as dirfd,
        _admin_record_lock(dirfd, record_path.parent),
    ):
        old = _read_protected(dirfd, record_path)
        if old is not None and (
            old["host_id"] != identity.host_id
            or old["installation_id"] != identity.installation_id
            or old["repository"] != repo
        ):
            raise ValueError("existing owner state belongs to another installation/repository")
        if action == "pair" and old is not None and old["state"] == "paired":
            raise ValueError("already paired; explicitly unpair before changing")
        if action == "unpair" and old is None:
            raise ValueError("no protected pairing state to revoke")
        if action == "unpair" and old is not None and old["state"] == "unpaired":
            return {
                "status": "already_unpaired", "paired": False, "epoch": old["epoch"],
                "standing_execution_enabled": False, "execution_authority": "not_asserted",
            }
        epoch = old["epoch"] + 1 if old else 1
        if epoch > 2**53:
            raise ValueError("protected pairing epoch exhausted")
        record = {
            "version": 1,
            "state": "paired" if action == "pair" else "unpaired",
            "repository": repo,
            "host_id": identity.host_id,
            "installation_id": identity.installation_id,
            "namespace": (
                f".agent/hosts/{identity.host_id}/supervisor/{identity.installation_id}"
            ),
            "epoch": epoch,
            "control_sha": expected_control_sha if action == "pair" else old["control_sha"],
            "standing_execution_enabled": False,
        }
        _atomic_protected_write(dirfd, record)
        return {
            "status": "protected_owner_choice_recorded" if action == "pair"
                      else "protected_owner_choice_revoked",
            "paired": action == "pair", "repository": repo,
            "host_id": identity.host_id,
            "installation_id": identity.installation_id,
            "epoch": epoch, "github_writers_verified": False,
            "standing_execution_enabled": False,
            "execution_authority": "not_asserted",
        }
