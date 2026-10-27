"""First-class manual child prepare/attach flow for the isolated DEV lab.

Manual preparation is read-only: the admitted ChildRequest is the durable authority and
its bootstrap is deterministic. Manual attach performs no browser effect and reuses the
same ChildRegistration/lifecycle contracts as automatic spawning.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from local_agent.conversation import bootstrap, contract
from local_agent.conversation.spawn_store import WorkflowConversationSpawnStore
from local_agent.conversation.store import WorkflowConversationStore
from local_agent.development.lab import DevLabLayout, build_dev_lab_layout, validate_dev_lab_layout
from local_agent.workflow.store import WorkflowStore

MANUAL_LIFECYCLE_SCHEMA_VERSION = 1


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _stores(
    layout: DevLabLayout,
    workflow_id: str,
) -> tuple[WorkflowConversationStore, WorkflowConversationSpawnStore]:
    validate_dev_lab_layout(layout)
    workflow_store = WorkflowStore(layout.state_dir)
    conversation_store = WorkflowConversationStore(workflow_store, workflow_id)
    return conversation_store, WorkflowConversationSpawnStore(conversation_store)


def prepare_manual_child(
    layout: DevLabLayout,
    workflow_id: str,
    request_id: str,
) -> dict[str, Any]:
    """Return the exact deterministic bootstrap without creating a spawn attempt."""
    conversation_store, spawn_store = _stores(layout, workflow_id)
    request = conversation_store.load_request(request_id)
    if request["workflow_id"] != workflow_id:
        raise RuntimeError("manual child request belongs to a different workflow")
    if conversation_store.load_registration(request_id) is not None:
        raise RuntimeError("manual child is already registered")

    lifecycle = conversation_store.load_state(request_id)
    if lifecycle["state"] not in {"requested", "registration_pending"}:
        raise RuntimeError(
            "manual child preparation requires requested or registration_pending state"
        )
    attempts = spawn_store.load_attempts(request_id)
    if attempts:
        raise RuntimeError(
            "manual child preparation requires zero spawn attempts; use spawn recovery instead"
        )

    bootstrap_text = bootstrap.child_bootstrap_message(request)
    return {
        "schema_version": MANUAL_LIFECYCLE_SCHEMA_VERSION,
        "workflow_id": workflow_id,
        "request_id": request["id"],
        "request_digest": contract.child_request_digest(request),
        "parent_conversation_url": request["parent_conversation_url"],
        "bootstrap_digest": bootstrap.child_bootstrap_digest(request),
        "bootstrap_text": bootstrap_text,
        "child_state": lifecycle["state"],
        "spawn_attempt_count": 0,
    }


def attach_manual_child(
    layout: DevLabLayout,
    workflow_id: str,
    request_id: str,
    *,
    child_conversation_url: str,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Attach one user-created child with no browser spawn transaction."""
    conversation_store, spawn_store = _stores(layout, workflow_id)
    request = conversation_store.load_request(request_id)
    if request["workflow_id"] != workflow_id:
        raise RuntimeError("manual child request belongs to a different workflow")

    registration = spawn_store.attach_manual_child(
        request_id,
        child_conversation_url=child_conversation_url,
        registered_at=_iso(now or _utc_now()),
    )
    lifecycle = conversation_store.load_state(request_id)
    return {
        "schema_version": MANUAL_LIFECYCLE_SCHEMA_VERSION,
        "status": "completed",
        "resolution": "manual_attach",
        "workflow_id": workflow_id,
        "request_id": request["id"],
        "request_digest": contract.child_request_digest(request),
        "child_conversation_url": registration["child_conversation_url"],
        "child_state": lifecycle["state"],
        "spawn_attempt_count": len(spawn_store.load_attempts(request_id)),
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
    parser.add_argument("command", choices=("prepare", "attach"))
    parser.add_argument("--workflow-id", required=True)
    parser.add_argument("--request-id", required=True)
    parser.add_argument("--child-conversation-url")
    parser.add_argument("--home")
    parser.add_argument("--root")
    parser.add_argument("--checkout")
    parser.add_argument("--production-checkout")
    return parser.parse_args(list(argv) if argv is not None else None)


def main(argv: Iterable[str] | None = None) -> int:
    try:
        args = parse_args(argv)
        layout = _layout_from_args(args)
        if args.command == "prepare":
            payload = prepare_manual_child(
                layout,
                str(args.workflow_id),
                str(args.request_id),
            )
        else:
            if not args.child_conversation_url:
                raise ValueError("--child-conversation-url is required for attach")
            payload = attach_manual_child(
                layout,
                str(args.workflow_id),
                str(args.request_id),
                child_conversation_url=str(args.child_conversation_url),
            )
        print(json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True))
        return 0
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"DEV manual lifecycle error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
