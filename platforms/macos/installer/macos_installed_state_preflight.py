"""Read-only Mac installed-state inventory. NOT a source-switch permit.

Git control checkouts may have repository permissions, while private local
claims need owner-only permissions. Never creates dirs, polls Git, acquires
runtime locks, changes launchd, or mutates/replays user tasks. Installed B2
does NOT cooperate with the proposed release-admission gate.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import stat
import sys
from typing import Any

from local_agent.repository.context import (
    repository_from_dict, validate_repository_set,
)
from local_agent.runtime.task_contract import _TASK_ID_RE

EXPECTED_BASE_SHA = "cccc67fce71db02d0e878172b1e96ce858ee96ea"
MAX_BYTES = 256 * 1024
MAX_LEDGER_ENTRIES = 1024
_NAME = re.compile(r"^[A-Za-z0-9._-]{1,128}\.json$")


class InstalledMacPreflightBlocked(ValueError):
    """An observational input is absent, ambiguous or untrusted."""


def _pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise InstalledMacPreflightBlocked("duplicate JSON field")
        result[key] = value
    return result


def _regular_json(path: Path, *, private: bool) -> dict[str, Any]:
    if path.is_symlink():
        raise InstalledMacPreflightBlocked("symlinked source evidence")
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except OSError as exc:
        raise InstalledMacPreflightBlocked("missing readable source evidence") from exc
    try:
        meta = os.fstat(fd)
        if (not stat.S_ISREG(meta.st_mode) or meta.st_uid != os.getuid()
                or meta.st_nlink != 1 or meta.st_size <= 0
                or meta.st_size > MAX_BYTES or (private and meta.st_mode & 0o077)):
            raise InstalledMacPreflightBlocked("invalid source evidence permissions or size")
        with os.fdopen(fd, "rb", closefd=False) as stream:
            raw = stream.read(MAX_BYTES + 1)
    finally:
        os.close(fd)
    if len(raw) > MAX_BYTES:
        raise InstalledMacPreflightBlocked("oversized source evidence")
    try:
        obj = json.loads(raw.decode("utf-8"), object_pairs_hook=_pairs)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise InstalledMacPreflightBlocked("malformed source evidence JSON") from exc
    if not isinstance(obj, dict):
        raise InstalledMacPreflightBlocked("JSON source evidence must be an object")
    return obj


def _safe_directory(path: Path, *, private: bool) -> None:
    if path.is_symlink():
        raise InstalledMacPreflightBlocked("symlinked directory")
    try:
        info = path.lstat()
    except OSError as exc:
        raise InstalledMacPreflightBlocked("missing observed directory") from exc
    if not stat.S_ISDIR(info.st_mode):
        raise InstalledMacPreflightBlocked("non-directory path")
    if private and (info.st_uid != os.getuid() or info.st_mode & 0o077):
        raise InstalledMacPreflightBlocked("private state directory not owner-only")


def _check_route_ancestors(base: Path, directory: Path, *, private: bool) -> None:
    """Never traverse redirected .agent or local-state directory ancestors.

    Checks each component *without resolving symlinks*. Observation may
    tolerate readable nonprivate ancestors but records the child directory
    separately as a release blocker; this is not deployment validation.
    """
    if not base.is_absolute() or directory != base and base not in directory.parents:
        raise InstalledMacPreflightBlocked("directory outside declared route")
    node = base
    for component in directory.relative_to(base).parts:
        node = node / component
        _safe_directory(node, private=False)
        info = node.lstat()
        if info.st_uid != os.getuid() or info.st_mode & 0o022:
            raise InstalledMacPreflightBlocked("unsafe route ancestor permissions")


def _count_records(directory: Path, *, private: bool, base: Path | None = None) -> tuple[int, tuple[str, ...]]:
    if base is not None:
        _check_route_ancestors(base, directory, private=private)
    _safe_directory(directory, private=private)
    count = 0
    names = []
    with os.scandir(directory) as iterator:
        for entry in iterator:
            count += 1
            if count > MAX_LEDGER_ENTRIES:
                raise InstalledMacPreflightBlocked("unbounded ledger directory")
            if entry.is_symlink():
                raise InstalledMacPreflightBlocked("ledger symlink entry")
            if entry.is_dir(follow_symlinks=False):
                raise InstalledMacPreflightBlocked("nested ledger directory")
            if not entry.is_file(follow_symlinks=False):
                raise InstalledMacPreflightBlocked("nonregular ledger entry")
            if not _NAME.fullmatch(entry.name):
                raise InstalledMacPreflightBlocked("non-json ledger file")
            names.append(entry.name)
    return count, tuple(names)


def _count_git_mailbox(
    directory: Path, *, base: Path, task_side: bool,
) -> tuple[int, tuple[str, ...], int, int]:
    """Recognize only exact source-supported Git mailbox envelope entries.

    Source proof: daemon polls top-level '*.json', while task_transport
    creates a '<task-id>.payload/' beside the *task* manifest. We do not
    read payload contents, traverse their trees or authorize task replay.
    """
    _check_route_ancestors(base, directory, private=False)
    _safe_directory(directory, private=False)
    count = 0
    names: list[str] = []
    metadata_count = 0
    payload_dirs = 0
    with os.scandir(directory) as iterator:
        for entry in iterator:
            count += 1
            if count > MAX_LEDGER_ENTRIES:
                raise InstalledMacPreflightBlocked("unbounded ledger directory")
            if entry.is_symlink():
                raise InstalledMacPreflightBlocked("ledger symlink entry")
            if entry.name == ".gitkeep":
                if not entry.is_file(follow_symlinks=False):
                    raise InstalledMacPreflightBlocked("invalid Git placeholder")
                info = entry.stat(follow_symlinks=False)
                if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                        or info.st_nlink != 1 or info.st_size != 0
                        or info.st_mode & 0o022):
                    raise InstalledMacPreflightBlocked("invalid Git placeholder")
                metadata_count += 1
                continue
            if entry.is_dir(follow_symlinks=False):
                prefix = entry.name.removesuffix(".payload")
                if (not task_side or not entry.name.endswith(".payload")
                        or not 1 <= len(prefix) <= 200
                        or _TASK_ID_RE.fullmatch(prefix) is None):
                    raise InstalledMacPreflightBlocked("unrecognized Git subdirectory")
                info = entry.stat(follow_symlinks=False)
                if info.st_uid != os.getuid() or info.st_mode & 0o022:
                    raise InstalledMacPreflightBlocked("unsafe Git payload directory")
                manifest = directory / f"{prefix}.json"
                if manifest.is_symlink() or not manifest.is_file():
                    raise InstalledMacPreflightBlocked("orphan Git payload directory")
                manifest_info = manifest.stat()
                if (not stat.S_ISREG(manifest_info.st_mode)
                        or manifest_info.st_uid != os.getuid()
                        or manifest_info.st_mode & 0o022):
                    raise InstalledMacPreflightBlocked("unsafe Git payload manifest")
                payload_dirs += 1
                continue
            if not entry.is_file(follow_symlinks=False):
                raise InstalledMacPreflightBlocked("nonregular ledger entry")
            if not (entry.name.endswith(".json")
                    and 1 <= len(entry.name[:-5]) <= 200
                    and _TASK_ID_RE.fullmatch(entry.name[:-5]) is not None):
                raise InstalledMacPreflightBlocked("non-json ledger file")
            names.append(entry.name)
    return len(names), tuple(names), metadata_count, payload_dirs


def _orphan_payload_tree_shape(path: Path) -> str:
    """Inspect only filesystem entry types, not contents or file names in output.

    Open each directory relative to a verified parent descriptor with
    O_NOFOLLOW. Files, unexpected links, deep or oversized trees are never
    read, deleted or treated as evidence that replay/cleanup is safe.
    """
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        root_fd = os.open(path, flags)
    except OSError:
        return "untrusted"
    stack = [(root_fd, 0)]
    entries = 0
    directories = 0
    files = 0
    try:
        while stack:
            fd, depth = stack.pop()
            try:
                meta = os.fstat(fd)
                if (not stat.S_ISDIR(meta.st_mode) or meta.st_uid != os.getuid()
                        or meta.st_mode & 0o022):
                    return "untrusted"
                with os.scandir(fd) as children:
                    for entry in children:
                        entries += 1
                        if entries > 2048:
                            return "untrusted"
                        meta = entry.stat(follow_symlinks=False)
                        if meta.st_uid != os.getuid() or meta.st_mode & 0o022:
                            return "untrusted"
                        if stat.S_ISDIR(meta.st_mode):
                            if depth >= 4:
                                return "untrusted"
                            child = os.open(entry.name, flags, dir_fd=fd)
                            stack.append((child, depth + 1))
                            directories += 1
                        elif stat.S_ISREG(meta.st_mode):
                            files += 1
                        else:
                            return "untrusted"
            finally:
                os.close(fd)
        if files:
            return "files_present"
        return "directories_only" if directories else "empty"
    except OSError:
        return "untrusted"
    finally:
        for fd, _depth in stack:
            os.close(fd)


def _count_unattached_task_payloads(
    directory: Path, *, base: Path,
) -> dict[str, int]:
    """Bounded, read-only orphan-root and entry-type census.

    A directory-only tree is consistent with a successful runtime cleanup
    that unlinked payload files but left empty ancestor directories. It
    does NOT prove that this was the origin of any particular live folder.
    """
    _check_route_ancestors(base, directory, private=False)
    _safe_directory(directory, private=False)
    results = {
        "total": 0, "empty": 0, "directories_only": 0,
        "files_present": 0, "untrusted": 0,
    }
    entries = 0
    with os.scandir(directory) as iterator:
        for entry in iterator:
            entries += 1
            if entries > MAX_LEDGER_ENTRIES:
                raise InstalledMacPreflightBlocked("unbounded ledger directory")
            if entry.is_symlink() or not entry.is_dir(follow_symlinks=False):
                continue
            if not entry.name.endswith(".payload"):
                continue
            prefix = entry.name.removesuffix(".payload")
            if not 1 <= len(prefix) <= 200 or _TASK_ID_RE.fullmatch(prefix) is None:
                continue
            manifest = directory / f"{prefix}.json"
            if manifest.is_symlink():
                raise InstalledMacPreflightBlocked("unsafe Git payload manifest")
            if not manifest.is_file():
                results["total"] += 1
                shape = _orphan_payload_tree_shape(Path(entry.path))
                results[shape] += 1
    return results


def _observe_private_records(
    directory: Path, *, base: Path,
) -> tuple[int, tuple[str, ...], bool]:
    """Read owner-controlled 0755 B2 ledger only as an *observation*.

    An owned, non-writable 0755 legacy directory can be observed without
    granting release trust. A group/other-writable directory or any symlink
    remains invalid. All returned nonprivate evidence is annotated.
    """
    try:
        count, names = _count_records(directory, private=True, base=base)
        return count, names, False
    except InstalledMacPreflightBlocked as exc:
        if str(exc) != "private state directory not owner-only":
            raise
        _safe_directory(directory, private=False)
        info = directory.lstat()
        if info.st_uid != os.getuid() or info.st_mode & 0o022:
            raise InstalledMacPreflightBlocked("unsafe private ledger write policy") from exc
        count, names = _count_records(directory, private=False, base=base)
        return count, names, True


def _gap_kind(exc: InstalledMacPreflightBlocked) -> str:
    """Safe aggregate diagnostic; never echo local paths or task contents."""
    reason = str(exc)
    if reason == "missing observed directory":
        return "missing_directory"
    if reason == "private state directory not owner-only":
        return "unsafe_directory_permissions"
    if reason == "non-json ledger file":
        return "non_json_metadata_file"
    if reason == "unrecognized Git subdirectory":
        return "unexpected_nested_directory"
    if reason == "orphan Git payload directory":
        return "orphan_payload_directory"
    if reason in {"invalid Git placeholder", "unsafe Git payload directory",
                  "unsafe Git payload manifest"}:
        return "unsafe_git_mailbox_auxiliary"
    if reason == "nested ledger directory":
        return "unexpected_nested_directory"
    if reason in {"symlinked directory", "ledger symlink entry"}:
        return "symlink_route_or_entry"
    if reason in {"nonregular ledger entry", "unbounded ledger directory",
                  "non-directory path", "directory outside declared route"}:
        return "unsafe_or_unbounded_ledger"
    if reason == "unsafe route ancestor permissions":
        return "unsafe_route_ancestor"
    return "unclassified_read_error"


def _raw_path_is_safe(raw: str, *, home: Path) -> bool:
    if raw.startswith("~/"):
        path = home / raw[2:]
    else:
        path = Path(raw)
    if not path.is_absolute():
        return False
    # Only inspect paths already present; do not normalize potentially
    # untrusted in-home symlinks into accepted new path identities.
    cursor = path
    while cursor != cursor.parent:
        if cursor.is_symlink():
            return False
        cursor = cursor.parent
    return True


def inspect_installed_mac(
    *, home: Path, expected_project_count: int,
    max_status_age_seconds: int = 180,
) -> dict[str, Any]:
    """Read-only, bounded inspection using real Mac registry routing.

    Counts are observational and intentionally do not constitute exclusive
    queue proof, actual Git remote freshness or release authorization.
    """
    if sys.platform != "darwin":
        raise InstalledMacPreflightBlocked("macOS-only installed-state probe")
    if (type(expected_project_count) is not int
            or not 1 <= expected_project_count <= 32):
        raise InstalledMacPreflightBlocked("explicit Mac project count required")
    if (type(max_status_age_seconds) is not int
            or not 1 <= max_status_age_seconds <= 3600):
        raise InstalledMacPreflightBlocked("invalid status age limit")
    home = Path(home).absolute()
    if home.is_symlink() or not home.is_dir():
        raise InstalledMacPreflightBlocked("unsafe Mac home")
    state = home / "Library/Application Support/local-agent"
    # The accepted B2 installation can have a 0755 state root. This is
    # acceptable for *read-only observation* only; capture it as an explicit
    # release blocker and never promote this preflight to switch authority.
    _safe_directory(state, private=False)
    state_info = state.lstat()
    if state_info.st_uid != os.getuid() or state_info.st_mode & 0o022:
        raise InstalledMacPreflightBlocked("Mac state root owner/write policy invalid")
    blockers: list[str] = []
    if state_info.st_mode & 0o077:
        blockers.append("state_root_not_private_for_release")
    registry_path = state / "repositories.json"
    registry = _regular_json(registry_path, private=False)
    registry_info = registry_path.lstat()
    if registry_info.st_mode & 0o022:
        raise InstalledMacPreflightBlocked("Mac registry writable by untrusted principals")
    if registry_info.st_mode & 0o077:
        blockers.append("registry_not_private_for_release")
    entries = registry.get("repositories")
    if (set(registry) != {"version", "repositories"}
            or type(registry.get("version")) is not int
            or registry["version"] != 1 or not isinstance(entries, list)
            or len(entries) != expected_project_count):
        raise InstalledMacPreflightBlocked("Mac registry inventory mismatch")
    if any(not isinstance(x, dict) or x.get("enabled", True) is not True for x in entries):
        raise InstalledMacPreflightBlocked("disabled/unexpected project registry entry")
    for entry in entries:
        for field in ("control_dir", "work_dir", "checkpoints_dir",
                      "host_identity_file"):
            value = entry.get(field)
            if value is not None and (
                not isinstance(value, str) or not _raw_path_is_safe(value, home=home)
            ):
                raise InstalledMacPreflightBlocked("unsafe Mac registry raw path")
    try:
        projects = validate_repository_set([
            repository_from_dict(x, home=home) for x in entries
        ])
    except ValueError as exc:
        raise InstalledMacPreflightBlocked("invalid Mac registry routing") from exc
    if len(projects) != expected_project_count:
        raise InstalledMacPreflightBlocked("Mac registry count changed")

    local_status = _regular_json(state / "status.json", private=False)
    fresh = False
    try:
        stamp = datetime.fromisoformat(local_status["updated_at"])
        if stamp.tzinfo is not None:
            age = (datetime.now(timezone.utc)
                   - stamp.astimezone(timezone.utc)).total_seconds()
            fresh = -5 <= age <= max_status_age_seconds
    except (ValueError, KeyError, TypeError):
        pass
    status_valid = (local_status.get("self_revision") == EXPECTED_BASE_SHA
                    and local_status.get("state") == "idle"
                    and type(local_status.get("pid")) is int
                    and local_status["pid"] > 0 and fresh)

    breakdown: dict[str, dict[str, Any]] = {}
    gap_causes: dict[str, int] = {}
    for project in projects:
        # Checked-out .agent/ files are NOT private task-state ledgers.
        control = project.control
        git_entry = control / ".git"
        if (git_entry.is_symlink()
                or not (git_entry.is_file() or git_entry.is_dir())):
            blockers.append("invalid_project_checkout")
        mailbox = control / ".agent"
        if project.host_mailbox_enabled:
            mailbox = mailbox / "hosts" / str(project.host_id)
        state_dir = state / "repositories" / project.repository_id
        if project.host_mailbox_enabled:
            state_dir = state_dir / "hosts" / str(project.host_id)
        counts: dict[str, int | None] = {}
        names: dict[str, tuple[str, ...]] = {}
        git_auxiliary: dict[str, int] = {}
        for key, folder, is_private in (
            ("tasks", mailbox / "tasks", False),
            ("results", mailbox / "results", False),
            ("claims", state_dir / "claims", True),
            ("spool", state_dir / "result-spool", True),
        ):
            try:
                if is_private:
                    counts[key], names[key], weaker_mode = _observe_private_records(
                        folder, base=state,
                    )
                    if weaker_mode:
                        blockers.append(f"project_{key}_mode_not_private_for_release")
                        gap_causes["readable_legacy_mode_not_release_private"] = (
                            gap_causes.get("readable_legacy_mode_not_release_private", 0) + 1
                        )
                else:
                    (counts[key], names[key], metadata, payloads) = _count_git_mailbox(
                        folder, base=control, task_side=(key == "tasks"),
                    )
                    git_auxiliary[f"{key}_placeholders"] = metadata
                    git_auxiliary[f"{key}_payload_directories"] = payloads
                    if payloads:
                        blockers.append("unverified_externalized_payload_contents")
            except InstalledMacPreflightBlocked as exc:
                counts[key] = None
                blockers.append(f"unverified_project_{key}")
                reason = _gap_kind(exc)
                if reason == "orphan_payload_directory" and key == "tasks":
                    try:
                        census = _count_unattached_task_payloads(folder, base=control)
                    except (InstalledMacPreflightBlocked, OSError):
                        census = {"total": 1, "untrusted": 1}
                    gap_causes[reason] = gap_causes.get(reason, 0) + max(
                        1, census["total"],
                    )
                    for shape in ("empty", "directories_only", "files_present", "untrusted"):
                        amount = census.get(shape, 0)
                        if amount:
                            field = f"orphan_payload_{shape}"
                            gap_causes[field] = gap_causes.get(field, 0) + amount
                else:
                    gap_causes[reason] = gap_causes.get(reason, 0) + 1
        if "tasks" in names and "results" in names:
            counts["pending"] = len(set(names["tasks"]) - set(names["results"]))
        else:
            counts["pending"] = None
        # No expensive result parsing or replay reconciliation in read-only
        # preflight. Even a published result can be UNKNOWN.
        breakdown[project.repository_id] = {
            "host_mailbox": project.host_mailbox_enabled,
            **counts,
            **git_auxiliary,
        }
    host_counts: dict[str, int | None] = {}
    for key in ("requests", "results", "claims", "remote-approvals"):
        try:
            host_counts[key], _, weaker_mode = _observe_private_records(
                state / "host-tasks" / key, base=state,
            )
            if weaker_mode:
                blockers.append(f"host_{key}_mode_not_private_for_release")
                gap_causes["readable_legacy_mode_not_release_private"] = (
                    gap_causes.get("readable_legacy_mode_not_release_private", 0) + 1
                )
        except InstalledMacPreflightBlocked as exc:
            host_counts[key] = None
            blockers.append(f"unverified_host_{key}")
            reason = _gap_kind(exc)
            gap_causes[reason] = gap_causes.get(reason, 0) + 1
    conversation_counts: dict[str, int | None] = {}
    for key in ("requests", "results", "launches"):
        try:
            conversation_counts[key], _, weaker_mode = _observe_private_records(
                state / "conversation-operator" / key, base=state,
            )
            if weaker_mode:
                blockers.append(f"conversation_{key}_mode_not_private_for_release")
                gap_causes["readable_legacy_mode_not_release_private"] = (
                    gap_causes.get("readable_legacy_mode_not_release_private", 0) + 1
                )
        except InstalledMacPreflightBlocked as exc:
            conversation_counts[key] = None
            blockers.append(f"unverified_conversation_{key}")
            reason = _gap_kind(exc)
            gap_causes[reason] = gap_causes.get(reason, 0) + 1
    config = state / "host-task-intake.json"
    if config.is_symlink():
        blockers.append("untrusted_host_intake_config")
    # No reading host credentials; presence cannot confirm enablement.
    host_intake = "absent" if not config.exists() and not config.is_symlink() else "unverified"
    if not status_valid:
        blockers.append("installed_supervisor_status_not_verified")
    return {
        "version": 1,
        "platform": "macos",
        "read_only": True,
        "project_count": len(projects),
        "installed_source_observed": local_status.get("self_revision"),
        "status_idle_fresh": status_valid,
        "projects": breakdown,
        "local_host": host_counts,
        "conversation": conversation_counts,
        "standalone_host_intake": host_intake,
        "blockers": sorted(set(blockers)),
        "observational_gap_causes": dict(sorted(gap_causes.items())),
        "git_origin_freshness_proven": False,
        "unknown_no_replay_proven": False,
        "installed_release_admission_proven": False,
        "source_switch_authorized": False,
        "actions_performed": [],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Read-only installed Mac evidence")
    parser.add_argument("--home", type=Path, required=True)
    parser.add_argument("--expected-project-count", type=int, required=True)
    args = parser.parse_args(argv)
    try:
        result = inspect_installed_mac(
            home=args.home, expected_project_count=args.expected_project_count,
        )
    except InstalledMacPreflightBlocked as exc:
        print(json.dumps({"read_only": True, "blocked": str(exc),
                          "source_switch_authorized": False}))
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
