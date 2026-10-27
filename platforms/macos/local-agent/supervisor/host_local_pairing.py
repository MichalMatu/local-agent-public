"""Source-only local owner pairing ledger; NEVER an executor authorization.

Records owner-reviewed GitHub host transport selection outside Git. The CLI
requires an interactive confirmation but a TTY alone does not cryptographically
attest local physical presence. No production intake imports this module.
"""
from __future__ import annotations

import contextlib
import fcntl
import json
import os
import stat
import tempfile
from pathlib import Path
from typing import Any, Iterator

from local_agent.foundation.process import fsync_directory
from local_agent.paths import repository_root
from local_agent.repository.host_facts import load_local_host_identity
from local_agent.repository.host_routing import HostIdentity, canonical_host_id
from local_agent.repository.path_safety import has_untrusted_symlink
from local_agent.supervisor.host_pairing_plan import (
    _canonical_repository, plan_private_github_pairing,
)
from local_agent.supervisor.host_private_producer import _require_private_directory

PAIRING_RECORD = "owner-pairing.json"
_LOCK_RECORD = "owner-pairing.lock"
_MAX_RECORD_BYTES = 4096
_FIELDS = frozenset({
    "version", "state", "repository", "host_id", "installation_id",
    "namespace", "epoch", "control_sha", "standing_execution_enabled",
})


def _state_dir(path: Path) -> Path:
    path = Path(path).expanduser()
    if (not path.is_absolute() or path == Path(path.anchor)
            or has_untrusted_symlink(path) or not path.is_dir()):
        raise ValueError("local pairing state directory must be an existing real absolute directory")
    _require_private_directory(path)
    real = path.resolve()
    source = repository_root().resolve()
    if (real == source or real in source.parents or source in real.parents
            or any((parent / ".git").exists()
                   for parent in (path, *path.parents))):
        raise ValueError("pairing state must be outside Git and installed source")
    return path


def _private_file(path: Path) -> None:
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077:
        raise ValueError("pairing record/lock must be a private regular file (0600)")
    if os.name == "posix" and info.st_uid != os.getuid():
        raise ValueError("pairing record/lock is not owned by the current OS user")
    if info.st_nlink != 1:
        raise ValueError("pairing record/lock must not be hard-linked")


def _record_from_bytes(raw: bytes) -> dict[str, Any]:
    if not 0 < len(raw) <= _MAX_RECORD_BYTES:
        raise ValueError("local pairing record exceeds bounded size")
    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for key, value in items:
            if key in out:
                raise ValueError("duplicate pairing record field")
            out[key] = value
        return out
    try:
        record = json.loads(raw.decode("utf-8"), object_pairs_hook=pairs)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("invalid local pairing JSON") from exc
    if not isinstance(record, dict) or set(record) != _FIELDS:
        raise ValueError("unsupported local pairing record schema")
    if type(record["version"]) is not int or record["version"] != 1:
        raise ValueError("unsupported local pairing version")
    if record["state"] not in ("paired", "unpaired"):
        raise ValueError("invalid local pairing state")
    if type(record["standing_execution_enabled"]) is not bool or record["standing_execution_enabled"]:
        raise ValueError("local pairing cannot enable standing execution")
    repo = _canonical_repository(record["repository"])
    if repo != record["repository"]:
        raise ValueError("pairing repository not canonical")
    identity = HostIdentity(record["host_id"], record["installation_id"])
    expected_namespace = (
        f".agent/hosts/{identity.host_id}/supervisor/{identity.installation_id}"
    )
    if record["namespace"] != expected_namespace:
        raise ValueError("pairing namespace differs from installation")
    epoch = record["epoch"]
    if type(epoch) is not int or not 1 <= epoch <= 2**53:
        raise ValueError("invalid local pairing epoch")
    sha = record["control_sha"]
    if (not isinstance(sha, str) or len(sha) != 40
            or any(char not in "0123456789abcdef" for char in sha)):
        raise ValueError("invalid local pairing control SHA")
    return record


def _read_record(path: Path) -> dict[str, Any] | None:
    if path.is_symlink():
        raise ValueError("pairing record cannot be a symlink")
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except FileNotFoundError:
        return None
    with os.fdopen(descriptor, "rb") as stream:
        info = os.fstat(stream.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077
                or info.st_nlink != 1 or info.st_size > _MAX_RECORD_BYTES
                or (os.name == "posix" and info.st_uid != os.getuid())):
            raise ValueError("pairing record must be private, bounded and regular")
        raw = stream.read(_MAX_RECORD_BYTES + 1)
    return _record_from_bytes(raw)


@contextlib.contextmanager
def _transaction(root: Path) -> Iterator[Path]:
    root = _state_dir(root)
    lock = root / _LOCK_RECORD
    fd = os.open(lock, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077
                or info.st_nlink != 1 or (os.name == "posix" and info.st_uid != os.getuid())):
            raise ValueError("pairing lock must be private and owned")
        fcntl.flock(fd, fcntl.LOCK_EX)
        if has_untrusted_symlink(root) or lock.is_symlink():
            raise ValueError("pairing lock/path changed during transaction")
        _private_file(lock)
        yield root / PAIRING_RECORD
    finally:
        os.close(fd)


def _write_record(path: Path, data: dict[str, Any], *, first: bool) -> None:
    encoded = (json.dumps(data, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
    _record_from_bytes(encoded)
    if path.is_symlink():
        raise ValueError("pairing record cannot be a symlink")
    if first:
        descriptor = os.open(
            path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        fsync_directory(path.parent)
        return
    _private_file(path)
    fd, name = tempfile.mkstemp(prefix=".owner-pairing.", suffix=".tmp", dir=path.parent)
    temp = Path(name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temp, 0o600)
        if path.is_symlink():
            raise ValueError("pairing state changed during replace")
        os.replace(temp, path)
        fsync_directory(path.parent)
    finally:
        temp.unlink(missing_ok=True)


def pairing_confirmation(repository: str, host_id: str) -> str:
    return f"PAIR {_canonical_repository(repository)} {canonical_host_id(host_id)}"


def unpair_confirmation(repository: str, host_id: str) -> str:
    return f"UNPAIR {_canonical_repository(repository)} {HostIdentity(host_id, '00000000-0000-4000-8000-000000000000').host_id}"


def pair_local_github_host(
    *, state_dir: Path, identity_file: Path, control_checkout: Path,
    repository: str, expected_host_id: str, expected_installation_id: str,
    expected_control_sha: str, owner_confirmation: str,
) -> dict[str, Any]:
    """Locally record explicit transport selection, with NO execution authority.

    This method is never used by remote worker intake. Confirmation is an
    application UX check, not a hardware/console presence attestation.
    """
    if owner_confirmation != pairing_confirmation(repository, expected_host_id):
        raise ValueError("explicit local pairing confirmation is missing")
    root = _state_dir(state_dir)
    identity = load_local_host_identity(identity_file)
    if identity != HostIdentity(expected_host_id, expected_installation_id):
        raise ValueError("local pairing identity differs from intended installation")
    if (root == Path(control_checkout).resolve() or root in Path(control_checkout).resolve().parents
            or Path(control_checkout).resolve() in root.parents):
        raise ValueError("local pairing state overlaps Git transport checkout")
    with _transaction(root) as file:
        prior = _read_record(file)
        if prior and prior["state"] == "paired":
            raise ValueError("local pairing already exists; unpair explicitly first")
        if prior and (prior["host_id"] != identity.host_id
                      or prior["installation_id"] != identity.installation_id):
            raise ValueError("prior pairing belongs to a different installation")
        evidence = plan_private_github_pairing(
            identity_file=identity_file, control_checkout=control_checkout,
            repository=repository, expected_host_id=identity.host_id,
            expected_installation_id=identity.installation_id,
            expected_control_sha=expected_control_sha,
        )
        epoch = 1 if prior is None else prior["epoch"] + 1
        if epoch > 2**53:
            raise ValueError("pairing epoch exhausted")
        record = {
            "version": 1, "state": "paired",
            "repository": evidence["repository"],
            "host_id": identity.host_id, "installation_id": identity.installation_id,
            "namespace": f".agent/hosts/{identity.host_id}/supervisor/{identity.installation_id}",
            "epoch": epoch, "control_sha": evidence["control_sha"],
            "standing_execution_enabled": False,
        }
        _write_record(file, record, first=prior is None)
        return {
            "status": "locally_paired_only", "paired": True, "repository": record["repository"],
            "host_id": identity.host_id, "installation_id": identity.installation_id,
            "epoch": epoch, "github_writers_verified": False,
            "standing_execution_enabled": False, "execution_authority": "not_asserted",
        }


def unpair_local_github_host(
    *, state_dir: Path, identity_file: Path, expected_repository: str,
    owner_confirmation: str,
) -> dict[str, Any]:
    """Durable offline local revocation marker, not remote/Git or worker control."""
    repo = _canonical_repository(expected_repository)
    if owner_confirmation != unpair_confirmation(repo, load_local_host_identity(identity_file).host_id):
        raise ValueError("explicit local unpair confirmation is missing")
    root = _state_dir(state_dir)
    with _transaction(root) as file:
        current = _read_record(file)
        if current is None:
            raise ValueError("no local owner pairing exists")
        identity = load_local_host_identity(identity_file)
        if (current["host_id"] != identity.host_id
                or current["installation_id"] != identity.installation_id
                or current["repository"] != repo):
            raise ValueError("local pairing identity/repository mismatch")
        if current["state"] == "unpaired":
            return {
                "status": "already_unpaired", "paired": False,
                "epoch": current["epoch"], "execution_authority": "not_asserted",
            }
        current["state"] = "unpaired"
        current["epoch"] += 1
        if current["epoch"] > 2**53:
            raise ValueError("pairing epoch exhausted")
        _write_record(file, current, first=False)
        return {
            "status": "locally_unpaired", "paired": False,
            "epoch": current["epoch"], "execution_authority": "not_asserted",
        }


def read_local_github_pairing(*, state_dir: Path, identity_file: Path) -> dict[str, Any]:
    """A local preview of owner choice; not an admission permit or live ACL check."""
    root = _state_dir(state_dir)
    with _transaction(root) as file:
        current = _read_record(file)
        if current is None:
            return {"status": "not_paired", "paired": False,
                    "standing_execution_enabled": False,
                    "execution_authority": "not_asserted"}
        identity = load_local_host_identity(identity_file)
        if (current["host_id"] != identity.host_id
                or current["installation_id"] != identity.installation_id):
            raise ValueError("pairing record targets another installation")
        return {
            "status": "locally_paired_only" if current["state"] == "paired"
                      else "locally_unpaired",
            "paired": current["state"] == "paired", "repository": current["repository"],
            "epoch": current["epoch"], "host_id": identity.host_id,
            "installation_id": identity.installation_id,
            "standing_execution_enabled": False, "execution_authority": "not_asserted",
        }
