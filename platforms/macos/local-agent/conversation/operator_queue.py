"""Local spool boundary for GitHub-backed Conversation Fabric operator requests/results."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from local_agent.conversation.operator_contract import (
    build_operator_result,
    load_operator_request,
    operator_request_digest,
    operator_result_bytes,
    validate_operator_result,
)
from local_agent.foundation.process import atomic_write_text, fsync_directory

CONTROL_REQUEST_DIR = Path(".agent/conversation/requests")
CONTROL_RESULT_DIR = Path(".agent/conversation/results")
LOCAL_SPOOL_DIR = "conversation-operator"
MAX_CONTROL_REQUEST_FILES = 128


@dataclass(frozen=True, slots=True)
class OperatorWorkItem:
    request_id: str
    request_digest: str
    request_path: Path
    result_path: Path


def _safe_child(root: Path, relative: Path) -> Path:
    resolved_root = root.resolve()
    target = (root / relative).resolve()
    if target == resolved_root or resolved_root not in target.parents:
        raise ValueError(f"operator control path escapes root: {relative}")
    return target


def _spool_dirs(state_dir: Path) -> tuple[Path, Path]:
    root = state_dir / LOCAL_SPOOL_DIR
    return root / "requests", root / "results"


def _spool_paths(state_dir: Path, request_id: str) -> tuple[Path, Path]:
    requests, results = _spool_dirs(state_dir)
    return requests / f"{request_id}.json", results / f"{request_id}.json"


def _request_bytes(request: dict[str, Any]) -> bytes:
    return json.dumps(request, indent=2, ensure_ascii=False, sort_keys=True).encode("utf-8") + b"\n"


def _load_json_regular(path: Path) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"operator JSON path is unavailable or unsafe: {path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"operator JSON path is invalid: {path}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"operator JSON path must contain an object: {path}")
    return payload


def _write_exact(path: Path, encoded: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise RuntimeError(f"operator spool path must not be a symlink: {path}")
    if path.exists():
        if not path.is_file() or path.read_bytes() != encoded:
            raise RuntimeError(f"operator spool conflicts with existing path: {path}")
        return
    atomic_write_text(path, encoded.decode("utf-8"))
    fsync_directory(path.parent)


def stage_next_control_request(control_root: Path, state_dir: Path) -> OperatorWorkItem | None:
    """Stage at most one validated remote request into local immutable spool state."""
    requests_dir, results_dir = _spool_dirs(state_dir)
    if requests_dir.exists():
        local = sorted(path for path in requests_dir.glob("*.json") if path.is_file())
        if local:
            request = load_operator_request(local[0])
            request_path, result_path = _spool_paths(state_dir, request["id"])
            item = OperatorWorkItem(
                request_id=request["id"],
                request_digest=operator_request_digest(request),
                request_path=request_path,
                result_path=result_path,
            )
            control_result = _safe_child(
                control_root, CONTROL_RESULT_DIR / f"{request['id']}.json"
            )
            if control_result.exists():
                published = _load_json_regular(control_result)
                validate_operator_result(published, request)
                if result_path.exists():
                    local_result = _load_json_regular(result_path)
                    validate_operator_result(local_result, request)
                    if local_result != published:
                        raise RuntimeError(
                            "published operator result conflicts with local durable result"
                        )
                discard_spool(item)
            else:
                return item

    control_requests = _safe_child(control_root, CONTROL_REQUEST_DIR)
    if not control_requests.exists():
        return None
    if control_requests.is_symlink() or not control_requests.is_dir():
        raise RuntimeError("operator control request directory is unsafe")
    paths = sorted(control_requests.glob("*.json"))
    if len(paths) > MAX_CONTROL_REQUEST_FILES:
        raise RuntimeError("operator control request directory exceeds file bound")

    for path in paths:
        request = load_operator_request(path)
        request_id = str(request["id"])
        if path.stem != request_id:
            raise RuntimeError("operator request filename must match immutable request id")
        control_result = _safe_child(control_root, CONTROL_RESULT_DIR / f"{request_id}.json")
        if control_result.exists():
            result = _load_json_regular(control_result)
            validate_operator_result(result, request)
            continue
        request_path, result_path = _spool_paths(state_dir, request_id)
        _write_exact(request_path, _request_bytes(request))
        return OperatorWorkItem(
            request_id=request_id,
            request_digest=operator_request_digest(request),
            request_path=request_path,
            result_path=result_path,
        )
    return None


def _launch_fence_path(state_dir: Path, request_id: str) -> Path:
    return state_dir / LOCAL_SPOOL_DIR / "launches" / f"{request_id}.json"


def launch_reconciliation_required(state_dir: Path, item: OperatorWorkItem) -> bool:
    """A previous launch attempt is ambiguous until its result is durably published."""
    path = _launch_fence_path(state_dir, item.request_id)
    return os.path.lexists(path)


def reserve_launch_once(state_dir: Path, item: OperatorWorkItem) -> bool:
    """Persist an exclusive launch fence *before* any browser-capable process spawns.

    A surviving fence means execution may already have happened. Its presence,
    even if incomplete or corrupt, never permits automatic replay. This fence
    is not a lock and does not grant execution rights.
    """
    path = _launch_fence_path(state_dir, item.request_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.parent.is_symlink():
        raise RuntimeError("operator launch fence directory is unsafe")
    # Persist both newly created directory entries before recording a one-shot
    # launch. The spool root and launches/ may both be new on the first request.
    fsync_directory(path.parent.parent.parent)
    fsync_directory(path.parent.parent)
    encoded = json.dumps(
        {
            "schema_version": 1,
            "request_id": item.request_id,
            "request_digest": item.request_digest,
        },
        sort_keys=True,
    ).encode("utf-8") + b"\n"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags, 0o600)
    except FileExistsError:
        return False
    with os.fdopen(fd, "wb") as stream:
        stream.write(encoded)
        stream.flush()
        os.fsync(stream.fileno())
    fsync_directory(path.parent)
    return True


def next_staged_request(state_dir: Path) -> OperatorWorkItem | None:
    requests_dir, _results_dir = _spool_dirs(state_dir)
    if not requests_dir.exists():
        return None
    paths = sorted(path for path in requests_dir.glob("*.json") if path.is_file())
    if not paths:
        return None
    request = load_operator_request(paths[0])
    request_path, result_path = _spool_paths(state_dir, request["id"])
    if result_path.is_file():
        return None
    return OperatorWorkItem(
        request_id=request["id"],
        request_digest=operator_request_digest(request),
        request_path=request_path,
        result_path=result_path,
    )


def persist_spooled_result(
    result_path: Path,
    request: dict[str, Any],
    result: dict[str, Any],
) -> dict[str, Any]:
    validate_operator_result(result, request)
    encoded = operator_result_bytes(result, request)
    _write_exact(result_path, encoded)
    return result


def persist_worker_failure(item: OperatorWorkItem, message: str) -> dict[str, Any]:
    request = load_operator_request(item.request_path)
    campaign = {
        "children": [
            {
                "request_id": child["request_id"],
                "child_state": "abandoned",
                "error": message,
                "child_conversation_url": None,
            }
            for child in request["children"]
        ]
    }
    result = build_operator_result(request, campaign)
    return persist_spooled_result(item.result_path, request, result)


def next_spooled_result(
    state_dir: Path,
) -> tuple[OperatorWorkItem, dict[str, Any], dict[str, Any]] | None:
    requests_dir, results_dir = _spool_dirs(state_dir)
    if not requests_dir.exists() or not results_dir.exists():
        return None
    for result_path in sorted(path for path in results_dir.glob("*.json") if path.is_file()):
        request_path = requests_dir / result_path.name
        request = load_operator_request(request_path)
        result = _load_json_regular(result_path)
        validate_operator_result(result, request)
        item = OperatorWorkItem(
            request_id=request["id"],
            request_digest=operator_request_digest(request),
            request_path=request_path,
            result_path=result_path,
        )
        return item, request, result
    return None


def require_control_request_match(
    control_root: Path,
    request: dict[str, Any],
) -> None:
    """Require the remote durable request to remain identical after admission."""
    path = _safe_child(control_root, CONTROL_REQUEST_DIR / f"{request['id']}.json")
    if not path.exists():
        raise RuntimeError("remote operator request disappeared after local staging")
    current = load_operator_request(path)
    if (
        operator_request_digest(current) != operator_request_digest(request)
        or current != request
    ):
        raise RuntimeError("remote operator request changed after local staging")


def control_result_relative(request_id: str) -> Path:
    return CONTROL_RESULT_DIR / f"{request_id}.json"


def control_result_matches(
    control_root: Path,
    request: dict[str, Any],
    result: dict[str, Any],
) -> bool:
    path = _safe_child(control_root, control_result_relative(request["id"]))
    if not path.exists():
        return False
    existing = _load_json_regular(path)
    validate_operator_result(existing, request)
    if existing != result:
        raise RuntimeError("published operator result conflicts with local durable result")
    return True


def discard_spool(item: OperatorWorkItem) -> None:
    parents: set[Path] = set()
    launch_path = (
        item.request_path.parent.parent / "launches" / f"{item.request_id}.json"
    )
    for path in (item.result_path, item.request_path, launch_path):
        try:
            path.unlink()
            parents.add(path.parent)
        except FileNotFoundError:
            pass
    for parent in parents:
        if parent.is_dir():
            fsync_directory(parent)
