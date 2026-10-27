"""One-shot real-ChatGPT plan for the isolated Conversation Fabric DEV lab.

The Stage 3 lab remains synthetic-only by default. This module adds a separate,
explicitly armed capability lease for one bounded live browser spawn. A live plan
is derived only from durable workflow/conversation state and one workflow-owned
SpawnTransaction. It never starts a Local Agent executor, installs Native
Messaging, or uses the operator's normal Chrome profile.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import secrets
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

from local_agent.conversation import bootstrap, contract, spawn
from local_agent.conversation.spawn_store import WorkflowConversationSpawnStore
from local_agent.conversation.store import WorkflowConversationStore
from local_agent.development.lab import (
    DevLabLayout,
    build_dev_lab_layout,
    dev_lab_status,
    validate_dev_lab_layout,
)
from local_agent.foundation.process import atomic_write_text, fsync_directory
from local_agent.workflow.store import WorkflowStore

LIVE_SLICE_SCHEMA_VERSION = 1
LIVE_SLICE_MODE = "real-chatgpt-bounded"
LIVE_SLICE_DIR_NAME = "live-slice"
LIVE_SLICE_PLAN_NAME = "plan.json"
LIVE_SLICE_ARM_NAME = "arm.json"
DEFAULT_ARM_TTL_SECONDS = 15 * 60
MAX_ARM_TTL_SECONDS = 30 * 60
MAX_ACTIVE_CHILDREN = 1
MAX_BROWSER_SPAWNS = 1
_ALLOWED_LIVE_TRANSACTION_STATES = frozenset(
    {
        "pending",
        "tab_created",
        "bootstrap_ready",
        "bootstrap_submitting",
        "identity_discovered",
        "registration_submitting",
    }
)


@dataclass(frozen=True, slots=True)
class LiveSlicePaths:
    root: Path
    plan: Path
    arm: Path
    browser_profile: Path
    evidence: Path


@dataclass(frozen=True, slots=True)
class LiveSliceAuthority:
    workflow_store: WorkflowStore
    conversation_store: WorkflowConversationStore
    spawn_store: WorkflowConversationSpawnStore
    request: dict[str, Any]
    lifecycle: dict[str, Any]
    transaction: dict[str, Any] | None


def _resolved(path: Path) -> Path:
    return path.expanduser().resolve(strict=False)


def _require_descendant(path: Path, root: Path, *, field: str) -> Path:
    resolved_path = _resolved(path)
    resolved_root = _resolved(root)
    if resolved_path == resolved_root or resolved_root not in resolved_path.parents:
        raise ValueError(f"live slice {field} must stay below DEV lab root: {resolved_path}")
    return resolved_path


def live_slice_paths(layout: DevLabLayout) -> LiveSlicePaths:
    validate_dev_lab_layout(layout)
    root = _require_descendant(
        layout.state_dir / LIVE_SLICE_DIR_NAME,
        layout.root,
        field="state root",
    )
    return LiveSlicePaths(
        root=root,
        plan=root / LIVE_SLICE_PLAN_NAME,
        arm=root / LIVE_SLICE_ARM_NAME,
        browser_profile=_require_descendant(
            layout.browser_profile_dir / LIVE_SLICE_DIR_NAME,
            layout.root,
            field="browser profile",
        ),
        evidence=_require_descendant(
            layout.logs_dir / LIVE_SLICE_DIR_NAME,
            layout.root,
            field="evidence directory",
        ),
    )


def _canonical_bytes(payload: Any) -> bytes:
    try:
        text = json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("live slice payload must be canonical JSON data") from exc
    return text.encode("utf-8")


def live_slice_plan_digest(plan: dict[str, Any]) -> str:
    if not isinstance(plan, dict):
        raise ValueError("live slice plan must be an object")
    return "sha256:" + hashlib.sha256(_canonical_bytes(plan)).hexdigest()


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_iso(value: Any, *, field: str) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise RuntimeError(f"live slice {field} must be an RFC3339 UTC timestamp")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise RuntimeError(f"live slice {field} is invalid") from exc
    if parsed.tzinfo != timezone.utc:
        raise RuntimeError(f"live slice {field} must use UTC")
    return parsed


def _require_initialized_lab(layout: DevLabLayout) -> None:
    status = dev_lab_status(layout)
    if not status["healthy"]:
        problems = ",".join(status["problems"])
        raise RuntimeError(f"DEV lab must be initialized and healthy before live slice: {problems}")


def _require_safe_directory(path: Path, root: Path, *, field: str) -> None:
    _require_descendant(path, root, field=field)
    if path.is_symlink():
        raise RuntimeError(f"live slice {field} must not be a symlink: {path}")
    if path.exists() and not path.is_dir():
        raise RuntimeError(f"live slice {field} must be a directory: {path}")


def _require_checkout_extension(layout: DevLabLayout) -> Path:
    extension = _resolved(layout.checkout / "chat_bridge")
    checkout = _resolved(layout.checkout)
    if extension == checkout or checkout not in extension.parents:
        raise ValueError("live slice extension must stay below DEV checkout")
    if extension.is_symlink() or not extension.is_dir():
        raise RuntimeError(f"DEV Chat Bridge extension is unavailable or unsafe: {extension}")
    manifest = extension / "manifest.json"
    if manifest.is_symlink() or not manifest.is_file():
        raise RuntimeError(f"DEV Chat Bridge manifest is unavailable or unsafe: {manifest}")
    return extension


def _stores(
    layout: DevLabLayout,
    workflow_id: str,
) -> tuple[WorkflowStore, WorkflowConversationStore, WorkflowConversationSpawnStore]:
    workflow_store = WorkflowStore(layout.state_dir)
    conversation_store = WorkflowConversationStore(workflow_store, workflow_id)
    spawn_store = WorkflowConversationSpawnStore(conversation_store)
    return workflow_store, conversation_store, spawn_store


def _validate_single_spawn_queue(
    spawn_store: WorkflowConversationSpawnStore,
    transaction: dict[str, Any] | None,
) -> None:
    snapshot = spawn_store.queue_snapshot()
    if snapshot["unresolved_ambiguous_ids"]:
        raise RuntimeError("unresolved ambiguous spawn blocks the bounded live slice")

    if transaction is None:
        if snapshot["active_transaction_id"] is not None or snapshot["pending_count"] != 0:
            raise RuntimeError("another conversation spawn already occupies the DEV live slice")
        return

    transaction_id = str(transaction["id"])
    state = str(transaction["state"])
    if state == "pending":
        if snapshot["active_transaction_id"] is not None:
            raise RuntimeError("another browser spawn is already active")
        if snapshot["pending_count"] != 1 or snapshot["pending_owner_id"] != transaction_id:
            raise RuntimeError("live spawn transaction does not exclusively own the DEV queue")
        return

    if state in spawn.BROWSER_ACTIVE_SPAWN_STATES:
        if snapshot["active_transaction_id"] != transaction_id:
            raise RuntimeError("live spawn transaction does not own browser-active state")
        if snapshot["pending_count"] != 0:
            raise RuntimeError("additional pending spawns violate the one-child live bound")
        return

    raise RuntimeError(f"live spawn transaction state is not recoverable: {state}")


def _load_live_authority(
    layout: DevLabLayout,
    workflow_id: str,
    request_id: str,
) -> LiveSliceAuthority:
    validate_dev_lab_layout(layout)
    _require_initialized_lab(layout)
    workflow_store, conversation_store, spawn_store = _stores(layout, workflow_id)
    request = conversation_store.load_request(request_id)
    contract.validate_child_request(request)
    if request["workflow_id"] != workflow_id:
        raise RuntimeError("durable child request belongs to a different workflow")
    if conversation_store.load_registration(request_id) is not None:
        raise RuntimeError("registered child cannot be prepared as a new live slice")

    lifecycle = conversation_store.load_state(request_id)
    if lifecycle["state"] not in {"requested", "registration_pending"}:
        raise RuntimeError(
            "live slice requires requested or registration_pending child state; "
            f"got {lifecycle['state']!r}"
        )

    attempts = spawn_store.load_attempts(request_id)
    if len(attempts) > MAX_BROWSER_SPAWNS:
        raise RuntimeError("bounded live slice forbids more than one spawn attempt")
    transaction = attempts[0] if attempts else None
    if transaction is not None:
        if transaction["attempt"] != 1:
            raise RuntimeError("bounded live slice requires spawn attempt 1")
        if transaction["state"] not in _ALLOWED_LIVE_TRANSACTION_STATES:
            raise RuntimeError(
                "existing spawn attempt is terminal or ambiguous; automatic live retry is forbidden"
            )
        expected_id = spawn.spawn_transaction_id(request, 1)
        if transaction["id"] != expected_id:
            raise RuntimeError("durable spawn transaction identity does not match child request")

    _validate_single_spawn_queue(spawn_store, transaction)
    return LiveSliceAuthority(
        workflow_store=workflow_store,
        conversation_store=conversation_store,
        spawn_store=spawn_store,
        request=request,
        lifecycle=lifecycle,
        transaction=transaction,
    )


def _transaction_identity(authority: LiveSliceAuthority) -> tuple[str, int]:
    transaction = authority.transaction
    if transaction is None:
        return spawn.spawn_transaction_id(authority.request, 1), 1
    return str(transaction["id"]), int(transaction["attempt"])


def _build_plan(
    layout: DevLabLayout,
    authority: LiveSliceAuthority,
) -> dict[str, Any]:
    paths = live_slice_paths(layout)
    extension = _require_checkout_extension(layout)
    _require_safe_directory(paths.browser_profile, layout.root, field="browser profile")
    _require_safe_directory(paths.evidence, layout.root, field="evidence directory")
    request = authority.request
    transaction_id, attempt = _transaction_identity(authority)

    return {
        "schema_version": LIVE_SLICE_SCHEMA_VERSION,
        "mode": LIVE_SLICE_MODE,
        "request": {
            "child_request_id": request["id"],
            "child_request_digest": contract.child_request_digest(request),
            "workflow_id": request["workflow_id"],
            "workflow_node_id": request["workflow_node_id"],
            "parent_conversation_url": request["parent_conversation_url"],
            "repository_id": request["repository_id"],
            "agent_binding": request["agent_binding"],
            "repository_ref": request["repository_ref"],
            "repository_commit_sha": request["repository_commit_sha"],
            "bootstrap_digest": bootstrap.child_bootstrap_digest(request),
            "bootstrap_text": bootstrap.child_bootstrap_message(request),
        },
        "spawn": {
            "transaction_id": transaction_id,
            "attempt": attempt,
            "authority": "workflow-conversation-spawn-store",
        },
        "paths": {
            "checkout": str(_resolved(layout.checkout)),
            "lab_root": str(_resolved(layout.root)),
            "state_dir": str(_resolved(layout.state_dir)),
            "browser_profile": str(paths.browser_profile),
            "extension": str(extension),
            "evidence": str(paths.evidence),
        },
        "limits": {
            "max_active_children": MAX_ACTIVE_CHILDREN,
            "max_browser_spawns": MAX_BROWSER_SPAWNS,
        },
        "capabilities": {
            "real_chatgpt_enabled": True,
            "browser_spawn_enabled": True,
            "executor_enabled": False,
            "remote_control_enabled": False,
            "native_host_registration_enabled": False,
            "production_chrome_profile_enabled": False,
        },
        "protected_operational_branches": ["chat-bridge-state", "operator-control"],
    }


def build_live_slice_plan(
    layout: DevLabLayout,
    workflow_id: str,
    request_id: str,
) -> dict[str, Any]:
    """Preview one bounded live plan from durable DEV workflow authority only."""
    authority = _load_live_authority(layout, workflow_id, request_id)
    return _build_plan(layout, authority)


def _plan_document(plan: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": LIVE_SLICE_SCHEMA_VERSION,
        "plan_digest": live_slice_plan_digest(plan),
        "plan": plan,
    }


def _json_text(payload: dict[str, Any]) -> str:
    return json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True) + "\n"


def _load_object(path: Path, *, field: str) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise RuntimeError(f"live slice {field} is missing or unsafe: {path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"live slice {field} is invalid: {path}") from exc
    if not isinstance(payload, dict):
        raise RuntimeError(f"live slice {field} must be a JSON object: {path}")
    return payload


def _validate_prepared_authority(layout: DevLabLayout, plan: dict[str, Any]) -> None:
    request_section = plan.get("request")
    spawn_section = plan.get("spawn")
    if not isinstance(request_section, dict) or not isinstance(spawn_section, dict):
        raise RuntimeError("live slice plan is missing durable request/spawn identity")
    workflow_id = request_section.get("workflow_id")
    request_id = request_section.get("child_request_id")
    if not isinstance(workflow_id, str) or not isinstance(request_id, str):
        raise RuntimeError("live slice plan workflow/request identity is invalid")

    authority = _load_live_authority(layout, workflow_id, request_id)
    if authority.transaction is None:
        raise RuntimeError("prepared live slice has no durable spawn transaction")
    expected = _build_plan(layout, authority)
    if expected != plan:
        raise RuntimeError("live slice plan no longer matches durable DEV authority")


def load_prepared_live_slice(layout: DevLabLayout) -> dict[str, Any]:
    _require_initialized_lab(layout)
    paths = live_slice_paths(layout)
    document = _load_object(paths.plan, field="plan")
    if document.get("schema_version") != LIVE_SLICE_SCHEMA_VERSION:
        raise RuntimeError("live slice plan schema version mismatch")
    plan = document.get("plan")
    digest = document.get("plan_digest")
    if not isinstance(plan, dict) or not isinstance(digest, str):
        raise RuntimeError("live slice plan document is incomplete")
    actual = live_slice_plan_digest(plan)
    if not secrets.compare_digest(digest, actual):
        raise RuntimeError("live slice plan digest mismatch")
    _validate_prepared_authority(layout, plan)
    return document


def prepare_live_slice(
    layout: DevLabLayout,
    workflow_id: str,
    request_id: str,
    *,
    created_at: str | None = None,
) -> dict[str, Any]:
    """Persist one exact attempt-1 plan without granting browser launch permission."""
    paths = live_slice_paths(layout)
    if paths.arm.exists() or paths.arm.is_symlink():
        raise RuntimeError("refusing to replace a live slice plan while an arm record exists")

    if paths.plan.exists() or paths.plan.is_symlink():
        existing = load_prepared_live_slice(layout)
        existing_request = existing["plan"]["request"]
        if (
            existing_request.get("workflow_id") != workflow_id
            or existing_request.get("child_request_id") != request_id
        ):
            raise RuntimeError("existing live slice plan belongs to a different durable request")
        return existing

    authority = _load_live_authority(layout, workflow_id, request_id)
    if authority.transaction is None:
        transaction = authority.spawn_store.enqueue(
            request_id,
            created_at=created_at or _iso(_utc_now()),
        )
        if transaction["attempt"] != 1 or transaction["state"] != "pending":
            raise RuntimeError("live slice prepare failed to create exact pending attempt 1")
        authority = _load_live_authority(layout, workflow_id, request_id)
    if authority.transaction is None:
        raise RuntimeError("live slice prepare did not establish durable spawn authority")

    plan = _build_plan(layout, authority)
    document = _plan_document(plan)
    _require_safe_directory(paths.root, layout.root, field="state root")
    paths.root.mkdir(parents=True, exist_ok=True)
    paths.browser_profile.mkdir(parents=True, exist_ok=True)
    paths.evidence.mkdir(parents=True, exist_ok=True)
    atomic_write_text(paths.plan, _json_text(document))
    return load_prepared_live_slice(layout)


def load_live_slice_arm(layout: DevLabLayout) -> dict[str, Any] | None:
    paths = live_slice_paths(layout)
    if not paths.arm.exists() and not paths.arm.is_symlink():
        return None
    arm = _load_object(paths.arm, field="arm record")
    if arm.get("schema_version") != LIVE_SLICE_SCHEMA_VERSION:
        raise RuntimeError("live slice arm schema version mismatch")
    if not isinstance(arm.get("plan_digest"), str):
        raise RuntimeError("live slice arm plan_digest is missing")
    nonce = arm.get("launch_nonce")
    if not isinstance(nonce, str) or len(nonce) != 32:
        raise RuntimeError("live slice arm launch_nonce is invalid")
    _parse_iso(arm.get("armed_at"), field="armed_at")
    _parse_iso(arm.get("expires_at"), field="expires_at")
    return arm


def arm_live_slice(
    layout: DevLabLayout,
    *,
    expected_plan_digest: str,
    ttl_seconds: int = DEFAULT_ARM_TTL_SECONDS,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Create a short-lived one-shot lease for the exact durable attempt-1 plan."""
    if type(ttl_seconds) is not int or not 1 <= ttl_seconds <= MAX_ARM_TTL_SECONDS:
        raise ValueError(f"arm ttl must be 1..{MAX_ARM_TTL_SECONDS} seconds")
    document = load_prepared_live_slice(layout)
    actual_digest = str(document["plan_digest"])
    if not secrets.compare_digest(str(expected_plan_digest), actual_digest):
        raise ValueError("live slice arm digest does not match prepared plan")

    current = (now or _utc_now()).astimezone(timezone.utc)
    existing = load_live_slice_arm(layout)
    if existing is not None:
        expires = _parse_iso(existing["expires_at"], field="expires_at")
        if expires > current:
            raise RuntimeError("live slice already has an active one-shot arm")

    arm = {
        "schema_version": LIVE_SLICE_SCHEMA_VERSION,
        "plan_digest": actual_digest,
        "launch_nonce": secrets.token_hex(16),
        "armed_at": _iso(current),
        "expires_at": _iso(current + timedelta(seconds=ttl_seconds)),
    }
    paths = live_slice_paths(layout)
    atomic_write_text(paths.arm, _json_text(arm))
    return arm


def consume_live_slice_arm(
    layout: DevLabLayout,
    *,
    launch_nonce: str,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Consume the one-shot lease before the first governed child-spawn effect."""
    document = load_prepared_live_slice(layout)
    arm = load_live_slice_arm(layout)
    if arm is None:
        raise RuntimeError("live slice is not armed")
    if not secrets.compare_digest(str(launch_nonce), arm["launch_nonce"]):
        raise ValueError("live slice launch nonce does not match active arm")
    if not secrets.compare_digest(str(document["plan_digest"]), arm["plan_digest"]):
        raise RuntimeError("live slice arm refers to a different prepared plan")

    current = (now or _utc_now()).astimezone(timezone.utc)
    expires = _parse_iso(arm["expires_at"], field="expires_at")
    if current >= expires:
        raise RuntimeError("live slice arm has expired")

    plan = document["plan"]
    request = plan["request"]
    authority = _load_live_authority(
        layout,
        str(request["workflow_id"]),
        str(request["child_request_id"]),
    )
    if authority.transaction is None or authority.transaction["state"] != "pending":
        raise RuntimeError("live slice arm can only authorize the original pending spawn")
    if authority.transaction["id"] != plan["spawn"]["transaction_id"]:
        raise RuntimeError("live slice durable spawn identity changed after arming")

    paths = live_slice_paths(layout)
    paths.arm.unlink()
    fsync_directory(paths.root)
    return document


def live_slice_status(
    layout: DevLabLayout,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    status = dev_lab_status(layout)
    if not status["healthy"]:
        return {
            "healthy": False,
            "prepared": False,
            "armed": False,
            "problems": list(status["problems"]),
        }

    paths = live_slice_paths(layout)
    problems: list[str] = []
    document: dict[str, Any] | None = None
    if paths.plan.exists() or paths.plan.is_symlink():
        try:
            document = load_prepared_live_slice(layout)
        except (RuntimeError, ValueError) as exc:
            problems.append(str(exc))

    arm: dict[str, Any] | None = None
    expired = False
    if paths.arm.exists() or paths.arm.is_symlink():
        try:
            arm = load_live_slice_arm(layout)
            if arm is not None:
                current = (now or _utc_now()).astimezone(timezone.utc)
                expired = current >= _parse_iso(arm["expires_at"], field="expires_at")
        except RuntimeError as exc:
            problems.append(str(exc))

    return {
        "healthy": not problems,
        "prepared": document is not None,
        "armed": arm is not None and not expired and document is not None,
        "expired": expired,
        "plan_digest": document.get("plan_digest") if document else None,
        "launch_nonce": arm.get("launch_nonce") if arm and not expired and document else None,
        "expires_at": arm.get("expires_at") if arm else None,
        "problems": problems,
        "plan": str(paths.plan),
        "arm": str(paths.arm),
    }


def _path_arg(value: str | None) -> Path | None:
    return None if value is None else Path(value)


def _layout_from_args(args: argparse.Namespace) -> DevLabLayout:
    return build_dev_lab_layout(
        home=_path_arg(args.home),
        root=_path_arg(args.root),
        checkout=_path_arg(args.checkout),
        production_checkout=_path_arg(args.production_checkout),
    )


def parse_args(argv: Iterable[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("plan", "prepare", "arm", "status"))
    parser.add_argument("--workflow-id")
    parser.add_argument("--request-id")
    parser.add_argument("--plan-digest")
    parser.add_argument("--ttl-seconds", type=int, default=DEFAULT_ARM_TTL_SECONDS)
    parser.add_argument("--home")
    parser.add_argument("--root")
    parser.add_argument("--checkout")
    parser.add_argument("--production-checkout")
    return parser.parse_args(list(argv) if argv is not None else None)


def _require_authority_args(args: argparse.Namespace) -> tuple[str, str]:
    workflow_id = args.workflow_id
    request_id = args.request_id
    if not workflow_id or not request_id:
        raise ValueError("--workflow-id and --request-id are required for plan/prepare")
    return str(workflow_id), str(request_id)


def main(argv: Iterable[str] | None = None) -> int:
    try:
        args = parse_args(argv)
        layout = _layout_from_args(args)
        if args.command == "plan":
            workflow_id, request_id = _require_authority_args(args)
            payload = _plan_document(build_live_slice_plan(layout, workflow_id, request_id))
        elif args.command == "prepare":
            workflow_id, request_id = _require_authority_args(args)
            payload = prepare_live_slice(layout, workflow_id, request_id)
        elif args.command == "arm":
            if not args.plan_digest:
                raise ValueError("--plan-digest is required for arm")
            payload = arm_live_slice(
                layout,
                expected_plan_digest=args.plan_digest,
                ttl_seconds=args.ttl_seconds,
            )
        else:
            payload = live_slice_status(layout)
        print(_json_text(payload), end="")
        return 0 if payload.get("healthy", True) else 1
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"DEV live slice error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
