from __future__ import annotations

import argparse
import json
import os
import secrets
import select
import shutil
import subprocess
import sys
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

from local_agent.conversation import contract
from local_agent.conversation.spawn_store import WorkflowConversationSpawnStore
from local_agent.conversation.store import WorkflowConversationStore
from local_agent.development.lab import DevLabLayout, build_dev_lab_layout, validate_dev_lab_layout
from local_agent.development.live_slice import (
    live_slice_paths,
    load_live_slice_arm,
    load_prepared_live_slice,
)
from local_agent.foundation.process import atomic_write_text, fsync_directory
from local_agent.workflow.store import WorkflowStore

LIVE_RUNNER_SCHEMA_VERSION = 1
LIVE_RUNNER_JOURNAL_NAME = "runner.json"
DEFAULT_BROWSER_TIMEOUT_SECONDS = 120
DEFAULT_LOGIN_TIMEOUT_SECONDS = 10 * 60
DEFAULT_LOGIN_PROBE_TIMEOUT_SECONDS = 20
DEFAULT_LOGIN_VERIFY_TIMEOUT_SECONDS = 30
CHILD_OBSERVE_BROWSER_TIMEOUT_SECONDS = 40
MAX_BROWSER_TIMEOUT_SECONDS = 30 * 60
MAX_PROTOCOL_LINE_CHARS = 128 * 1024
MAX_LIVE_BOOTSTRAP_CHARS = 32_768
_RECOVERABLE_TRANSACTION_STATES = frozenset(
    {
        "pending",
        "tab_created",
        "bootstrap_ready",
        "bootstrap_submitting",
        "identity_discovered",
        "registration_submitting",
    }
)


class BrowserSession(Protocol):
    def wait_ready(self, *, timeout_seconds: int) -> dict[str, Any]: ...

    def create(self, intent: dict[str, Any]) -> dict[str, Any]: ...

    def recover_create(self, intent: dict[str, Any]) -> dict[str, Any]: ...

    def reattach_pre_submit(self, intent: dict[str, Any]) -> dict[str, Any]: ...

    def probe(self, intent: dict[str, Any]) -> dict[str, Any]: ...

    def submit(self, intent: dict[str, Any]) -> dict[str, Any]: ...

    def reconcile(self, intent: dict[str, Any]) -> dict[str, Any]: ...

    def open_child(self, child_conversation_url: str, ownership: dict[str, Any] | None = None) -> dict[str, Any]: ...

    def observe_child(self, child_conversation_url: str, ownership: dict[str, Any] | None = None) -> dict[str, Any]: ...

    def close_child(self, child_conversation_url: str, ownership: dict[str, Any] | None = None) -> dict[str, Any]: ...

    def close(self) -> None: ...


@dataclass(frozen=True, slots=True)
class RunnerAuthority:
    document: dict[str, Any]
    workflow_store: WorkflowStore
    conversation_store: WorkflowConversationStore
    spawn_store: WorkflowConversationSpawnStore
    request: dict[str, Any]
    transaction: dict[str, Any]


@dataclass(frozen=True, slots=True)
class RunnerPaths:
    journal: Path
    evidence: Path


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_iso(value: Any, *, field: str) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z") or len(value) > 64:
        raise RuntimeError(f"live runner {field} must be an RFC3339 UTC timestamp")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise RuntimeError(f"live runner {field} is invalid") from exc
    if parsed.tzinfo != timezone.utc:
        raise RuntimeError(f"live runner {field} must use UTC")
    return parsed


def _json_text(payload: dict[str, Any]) -> str:
    return json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True) + "\n"


def _resolved(path: Path) -> Path:
    return path.expanduser().resolve(strict=False)


def _require_descendant(path: Path, root: Path, *, field: str) -> Path:
    resolved_path = _resolved(path)
    resolved_root = _resolved(root)
    if resolved_path == resolved_root or resolved_root not in resolved_path.parents:
        raise RuntimeError(f"live runner {field} must stay below DEV lab root: {resolved_path}")
    return resolved_path


def runner_paths(layout: DevLabLayout) -> RunnerPaths:
    live_paths = live_slice_paths(layout)
    return RunnerPaths(
        journal=live_paths.root / LIVE_RUNNER_JOURNAL_NAME,
        evidence=_require_descendant(
            live_paths.evidence / "result.json",
            layout.root,
            field="evidence result",
        ),
    )


def _shared_dev_node_deps(layout: DevLabLayout) -> Path:
    return _resolved(
        layout.home / "Library" / "Application Support" / "local-agent-dev" / "node-deps"
    )


def _chrome_version_key(path: Path) -> tuple[int, ...]:
    prefix = "chrome-for-testing-"
    name = next((part for part in path.parts if part.startswith(prefix)), "")
    raw = name[len(prefix):]
    try:
        return tuple(int(part) for part in raw.split("."))
    except ValueError:
        return ()


def _browser_environment(layout: DevLabLayout) -> dict[str, str]:
    """Build browser actuator environment with shared DEV tooling auto-discovery.

    Explicit operator overrides always win. Otherwise use the already-installed shared
    local-agent-dev Playwright package and newest valid Chrome for Testing bundle.
    """
    env = os.environ.copy()
    deps = _shared_dev_node_deps(layout)

    if not str(env.get("LOCAL_AGENT_PLAYWRIGHT_MODULE", "")).strip():
        playwright = deps / "node_modules" / "playwright"
        if playwright.is_dir() and not playwright.is_symlink():
            env["LOCAL_AGENT_PLAYWRIGHT_MODULE"] = str(playwright)

    if not str(env.get("LOCAL_AGENT_CHROME_EXECUTABLE", "")).strip() and sys.platform == "darwin":
        suffix = Path(
            "chrome-mac-arm64/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing"
        )
        candidates = []
        if deps.is_dir() and not deps.is_symlink():
            for bundle in deps.glob("chrome-for-testing-*"):
                candidate = bundle / suffix
                if candidate.is_file() and not candidate.is_symlink() and os.access(candidate, os.X_OK):
                    candidates.append(candidate)
        if candidates:
            selected = max(candidates, key=lambda item: (_chrome_version_key(item), str(item)))
            env["LOCAL_AGENT_CHROME_EXECUTABLE"] = str(_resolved(selected))

    return env


def _load_object(path: Path, *, field: str) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise RuntimeError(f"live runner {field} is missing or unsafe: {path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"live runner {field} is invalid: {path}") from exc
    if not isinstance(payload, dict):
        raise RuntimeError(f"live runner {field} must be a JSON object")
    return payload


def load_runner_journal(layout: DevLabLayout) -> dict[str, Any] | None:
    path = runner_paths(layout).journal
    if not path.exists() and not path.is_symlink():
        return None
    payload = _load_object(path, field="journal")
    required = {
        "schema_version",
        "plan_digest",
        "workflow_id",
        "request_id",
        "transaction_id",
        "phase",
        "updated_at",
    }
    if set(payload) != required:
        raise RuntimeError("live runner journal fields do not match schema")
    if payload["schema_version"] != LIVE_RUNNER_SCHEMA_VERSION:
        raise RuntimeError("live runner journal schema version mismatch")
    for field in ("plan_digest", "workflow_id", "request_id", "transaction_id", "phase"):
        if not isinstance(payload[field], str) or not payload[field]:
            raise RuntimeError(f"live runner journal {field} is invalid")
    _parse_iso(payload["updated_at"], field="journal updated_at")
    return payload


def _write_journal(
    layout: DevLabLayout,
    *,
    document: dict[str, Any],
    phase: str,
    now: datetime,
) -> dict[str, Any]:
    plan = document["plan"]
    payload = {
        "schema_version": LIVE_RUNNER_SCHEMA_VERSION,
        "plan_digest": document["plan_digest"],
        "workflow_id": plan["request"]["workflow_id"],
        "request_id": plan["request"]["child_request_id"],
        "transaction_id": plan["spawn"]["transaction_id"],
        "phase": phase,
        "updated_at": _iso(now),
    }
    path = runner_paths(layout).journal
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(path, _json_text(payload))
    return payload


def _stores(
    layout: DevLabLayout,
    workflow_id: str,
) -> tuple[WorkflowStore, WorkflowConversationStore, WorkflowConversationSpawnStore]:
    workflow_store = WorkflowStore(layout.state_dir)
    conversation_store = WorkflowConversationStore(workflow_store, workflow_id)
    spawn_store = WorkflowConversationSpawnStore(conversation_store)
    return workflow_store, conversation_store, spawn_store


def _load_authority_from_document(
    layout: DevLabLayout,
    document: dict[str, Any],
) -> RunnerAuthority:
    plan = document.get("plan")
    if not isinstance(plan, dict):
        raise RuntimeError("live runner plan is missing")
    request_section = plan.get("request")
    spawn_section = plan.get("spawn")
    if not isinstance(request_section, dict) or not isinstance(spawn_section, dict):
        raise RuntimeError("live runner plan authority is incomplete")

    workflow_id = str(request_section.get("workflow_id") or "")
    request_id = str(request_section.get("child_request_id") or "")
    if not workflow_id or not request_id:
        raise RuntimeError("live runner workflow/request identity is invalid")
    workflow_store, conversation_store, spawn_store = _stores(layout, workflow_id)
    request = conversation_store.load_request(request_id)
    contract.validate_child_request(request)
    if contract.child_request_digest(request) != request_section.get("child_request_digest"):
        raise RuntimeError("live runner request digest no longer matches durable request")

    attempts = spawn_store.load_attempts(request_id)
    if len(attempts) != 1:
        raise RuntimeError("live runner requires exactly one durable spawn attempt")
    transaction = attempts[0]
    if transaction["attempt"] != 1:
        raise RuntimeError("live runner requires durable spawn attempt 1")
    if transaction["id"] != spawn_section.get("transaction_id"):
        raise RuntimeError("live runner spawn identity no longer matches prepared plan")
    if transaction["state"] not in _RECOVERABLE_TRANSACTION_STATES:
        raise RuntimeError(
            f"live runner cannot automatically continue transaction state {transaction['state']!r}"
        )
    return RunnerAuthority(
        document=document,
        workflow_store=workflow_store,
        conversation_store=conversation_store,
        spawn_store=spawn_store,
        request=request,
        transaction=transaction,
    )


def _consume_runner_arm(
    layout: DevLabLayout,
    *,
    launch_nonce: str,
    now: datetime,
) -> RunnerAuthority:
    document = load_prepared_live_slice(layout)
    arm = load_live_slice_arm(layout)
    if arm is None:
        raise RuntimeError("live slice is not armed")
    if not secrets.compare_digest(str(launch_nonce), str(arm["launch_nonce"])):
        raise ValueError("live runner launch nonce does not match active arm")
    if not secrets.compare_digest(str(document["plan_digest"]), str(arm["plan_digest"])):
        raise RuntimeError("live runner arm refers to a different prepared plan")
    if now >= _parse_iso(arm["expires_at"], field="arm expires_at"):
        raise RuntimeError("live runner arm has expired")

    authority = _load_authority_from_document(layout, document)
    arm_path = live_slice_paths(layout).arm
    arm_path.unlink()
    fsync_directory(arm_path.parent)
    return authority


def _intent(authority: RunnerAuthority) -> dict[str, Any]:
    plan = authority.document["plan"]
    request = plan["request"]
    bootstrap_text = str(request["bootstrap_text"])
    if len(bootstrap_text) > MAX_LIVE_BOOTSTRAP_CHARS:
        raise RuntimeError(
            f"live runner bootstrap exceeds browser bound {MAX_LIVE_BOOTSTRAP_CHARS} characters"
        )
    transaction = authority.transaction
    return {
        "schema_version": 1,
        "transaction_id": transaction["id"],
        "child_request_digest": request["child_request_digest"],
        "bootstrap_digest": request["bootstrap_digest"],
        "bootstrap_text": bootstrap_text,
        "tab_id": transaction["tab_id"],
    }


def _validate_result(result: Any, *, action: str) -> dict[str, Any]:
    if not isinstance(result, dict):
        raise RuntimeError(f"live browser {action} returned a non-object response")
    if type(result.get("ok")) is not bool:
        raise RuntimeError(f"live browser {action} response is missing boolean ok")
    reason = result.get("reason")
    if not isinstance(reason, str) or not reason:
        raise RuntimeError(f"live browser {action} response is missing reason")
    return result


def _refresh(authority: RunnerAuthority) -> RunnerAuthority:
    return RunnerAuthority(
        document=authority.document,
        workflow_store=authority.workflow_store,
        conversation_store=authority.conversation_store,
        spawn_store=authority.spawn_store,
        request=authority.request,
        transaction=authority.spawn_store.load_attempt(authority.request["id"], 1),
    )


def _registration(authority: RunnerAuthority, *, registered_at: str) -> dict[str, Any]:
    child_url = authority.transaction.get("child_conversation_url")
    if not isinstance(child_url, str):
        raise RuntimeError("live runner durable child URL is missing before registration")
    canonical = contract.canonical_conversation_url(child_url)
    if canonical != child_url:
        raise RuntimeError("live runner durable child URL is not canonical")
    request = authority.request
    return {
        "schema_version": contract.CHILD_REGISTRATION_SCHEMA_VERSION,
        "child_request_id": request["id"],
        "child_request_digest": contract.child_request_digest(request),
        "parent_conversation_url": request["parent_conversation_url"],
        "child_conversation_url": child_url,
        "registered_at": registered_at,
    }


def _write_completion_evidence(
    layout: DevLabLayout,
    authority: RunnerAuthority,
    *,
    now: datetime,
) -> dict[str, Any]:
    registration = authority.conversation_store.load_registration(authority.request["id"])
    if registration is None:
        raise RuntimeError("live runner completion requires durable child registration")
    transaction = authority.spawn_store.load_attempt(authority.request["id"], 1)
    if transaction["state"] != "done":
        raise RuntimeError("live runner completion requires done spawn transaction")
    lifecycle = authority.conversation_store.load_state(authority.request["id"])
    payload = {
        "schema_version": LIVE_RUNNER_SCHEMA_VERSION,
        "completed_at": _iso(now),
        "plan_digest": authority.document["plan_digest"],
        "workflow_id": authority.request["workflow_id"],
        "request_id": authority.request["id"],
        "transaction_id": transaction["id"],
        "spawn_state": transaction["state"],
        "child_state": lifecycle["state"],
        "child_conversation_url": registration["child_conversation_url"],
    }
    paths = runner_paths(layout)
    paths.evidence.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(paths.evidence, _json_text(payload))
    for path in (paths.journal, live_slice_paths(layout).plan, live_slice_paths(layout).arm):
        if path.exists() and not path.is_symlink():
            path.unlink()
    fsync_directory(paths.evidence.parent)
    return payload


def abandon_ambiguous_live_slice(
    layout: DevLabLayout,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Explicitly retire one unrecoverable ambiguous child without spawning again."""
    validate_dev_lab_layout(layout)
    current = (now or _utc_now()).astimezone(timezone.utc)
    journal = load_runner_journal(layout)
    if journal is None or journal.get("phase") != "ambiguous":
        raise RuntimeError("ambiguous abandonment requires an ambiguous runner journal")

    workflow_store, conversation_store, spawn_store = _stores(
        layout, str(journal["workflow_id"])
    )
    request = conversation_store.load_request(str(journal["request_id"]))
    attempts = spawn_store.load_attempts(request["id"])
    if len(attempts) != 1:
        raise RuntimeError("ambiguous abandonment requires exactly one spawn attempt")
    transaction = attempts[0]
    if transaction["id"] != journal["transaction_id"]:
        raise RuntimeError("ambiguous abandonment journal conflicts with spawn identity")
    if transaction["state"] != "ambiguous":
        raise RuntimeError("ambiguous abandonment requires an ambiguous spawn transaction")
    if conversation_store.load_registration(request["id"]) is not None:
        raise RuntimeError("registered ambiguous child must be reconciled, not abandoned")

    lifecycle = spawn_store.abandon_ambiguous(
        request["id"],
        1,
        updated_at=_iso(current),
    )
    durable = spawn_store.load_attempt(request["id"], 1)
    if durable["state"] != "ambiguous":
        raise RuntimeError("ambiguous abandonment must preserve ambiguous spawn evidence")
    if durable["id"] in spawn_store.queue_snapshot()["unresolved_ambiguous_ids"]:
        raise RuntimeError("ambiguous abandonment did not release global spawn blocking")

    payload = {
        "schema_version": LIVE_RUNNER_SCHEMA_VERSION,
        "completed_at": _iso(current),
        "plan_digest": journal["plan_digest"],
        "workflow_id": request["workflow_id"],
        "request_id": request["id"],
        "transaction_id": durable["id"],
        "spawn_state": durable["state"],
        "child_state": lifecycle["state"],
        "resolution": "abandoned_unresolved",
        "failure_reason": durable["failure_reason"],
    }
    paths = runner_paths(layout)
    paths.evidence.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(paths.evidence, _json_text(payload))
    for path in (paths.journal, live_slice_paths(layout).plan, live_slice_paths(layout).arm):
        if path.exists() and not path.is_symlink():
            path.unlink()
    fsync_directory(paths.evidence.parent)
    return {"status": "completed", "abandoned": True, **payload}


def attach_ambiguous_live_slice(
    layout: DevLabLayout,
    *,
    child_conversation_url: str,
    now: datetime | None = None,
) -> dict[str, Any]:
    # Explicit operator recovery for one already-created child after ambiguous submit.
    # This never launches a browser and never retries submission.
    validate_dev_lab_layout(layout)
    current = (now or _utc_now()).astimezone(timezone.utc)
    journal = load_runner_journal(layout)
    if journal is None or journal.get("phase") != "ambiguous":
        raise RuntimeError("manual live attach requires an ambiguous runner journal")

    workflow_store, conversation_store, spawn_store = _stores(
        layout, str(journal["workflow_id"])
    )
    request = conversation_store.load_request(str(journal["request_id"]))
    attempts = spawn_store.load_attempts(request["id"])
    if len(attempts) != 1:
        raise RuntimeError("manual live attach requires exactly one spawn attempt")
    transaction = attempts[0]
    if transaction["id"] != journal["transaction_id"]:
        raise RuntimeError("manual live attach journal conflicts with spawn identity")
    if transaction["state"] != "ambiguous":
        raise RuntimeError("manual live attach requires an ambiguous spawn transaction")
    lifecycle = conversation_store.load_state(request["id"])
    existing_registration = conversation_store.load_registration(request["id"])
    if lifecycle["state"] not in {"registration_pending", "active"}:
        raise RuntimeError(
            "manual live attach requires registration_pending or active child state"
        )

    canonical = contract.canonical_conversation_url(str(child_conversation_url))
    if canonical != str(child_conversation_url):
        raise ValueError("manual live attach requires canonical child conversation URL")
    if canonical == request["parent_conversation_url"]:
        raise ValueError("manual live attach child URL must differ from parent")

    registration = {
        "schema_version": contract.CHILD_REGISTRATION_SCHEMA_VERSION,
        "child_request_id": request["id"],
        "child_request_digest": contract.child_request_digest(request),
        "parent_conversation_url": request["parent_conversation_url"],
        "child_conversation_url": canonical,
        "registered_at": (
            existing_registration["registered_at"]
            if existing_registration is not None
            else _iso(current)
        ),
    }
    conversation_store.register_child(registration)
    lifecycle = conversation_store.load_state(request["id"])
    snapshot = spawn_store.queue_snapshot()
    if transaction["id"] in snapshot["unresolved_ambiguous_ids"]:
        raise RuntimeError("manual live attach did not resolve queue ambiguity")

    payload = {
        "schema_version": LIVE_RUNNER_SCHEMA_VERSION,
        "completed_at": _iso(current),
        "plan_digest": journal["plan_digest"],
        "workflow_id": request["workflow_id"],
        "request_id": request["id"],
        "transaction_id": transaction["id"],
        "spawn_state": transaction["state"],
        "child_state": lifecycle["state"],
        "child_conversation_url": canonical,
        "resolution": "manual_attach",
        "failure_reason": transaction["failure_reason"],
    }
    paths = runner_paths(layout)
    paths.evidence.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(paths.evidence, _json_text(payload))
    for path in (paths.journal, live_slice_paths(layout).plan, live_slice_paths(layout).arm):
        if path.exists() and not path.is_symlink():
            path.unlink()
    fsync_directory(paths.evidence.parent)
    return {"status": "completed", "recovered": True, **payload}


def _recover_registered_completion(
    layout: DevLabLayout,
    *,
    now: datetime,
) -> dict[str, Any] | None:
    journal = load_runner_journal(layout)
    if journal is None:
        return None
    workflow_store, conversation_store, spawn_store = _stores(layout, journal["workflow_id"])
    request = conversation_store.load_request(journal["request_id"])
    registration = conversation_store.load_registration(request["id"])
    if registration is None:
        return None
    attempts = spawn_store.load_attempts(request["id"])
    if len(attempts) != 1 or attempts[0]["id"] != journal["transaction_id"]:
        raise RuntimeError("live runner journal conflicts with durable spawn history")
    transaction = attempts[0]
    if transaction["state"] == "registration_submitting":
        spawn_store.advance(
            request["id"],
            1,
            target="done",
            updated_at=_iso(now),
        )
    elif transaction["state"] == "ambiguous" and journal.get("phase") == "ambiguous":
        lifecycle = conversation_store.load_state(request["id"])
        if lifecycle["state"] == "registration_pending":
            conversation_store.register_child(registration)
            lifecycle = conversation_store.load_state(request["id"])
        if lifecycle["state"] != "active":
            raise RuntimeError(
                "registered ambiguous recovery requires active child state"
            )
        payload = {
            "schema_version": LIVE_RUNNER_SCHEMA_VERSION,
            "completed_at": _iso(now),
            "plan_digest": journal["plan_digest"],
            "workflow_id": request["workflow_id"],
            "request_id": request["id"],
            "transaction_id": transaction["id"],
            "spawn_state": transaction["state"],
            "child_state": lifecycle["state"],
            "child_conversation_url": registration["child_conversation_url"],
            "resolution": "manual_attach",
            "failure_reason": transaction["failure_reason"],
        }
        paths = runner_paths(layout)
        paths.evidence.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(paths.evidence, _json_text(payload))
        for cleanup_path in (
            paths.journal,
            live_slice_paths(layout).plan,
            live_slice_paths(layout).arm,
        ):
            if cleanup_path.exists() and not cleanup_path.is_symlink():
                cleanup_path.unlink()
        fsync_directory(paths.evidence.parent)
        return payload
    elif transaction["state"] != "done":
        raise RuntimeError(
            "durable child registration exists outside recoverable registration_submitting state"
        )

    document = {
        "plan_digest": journal["plan_digest"],
        "plan": {
            "request": {
                "workflow_id": journal["workflow_id"],
                "child_request_id": journal["request_id"],
            },
            "spawn": {"transaction_id": journal["transaction_id"]},
        },
    }
    authority = RunnerAuthority(
        document=document,
        workflow_store=workflow_store,
        conversation_store=conversation_store,
        spawn_store=spawn_store,
        request=request,
        transaction=spawn_store.load_attempt(request["id"], 1),
    )
    return _write_completion_evidence(layout, authority, now=now)


def _result(status: str, authority: RunnerAuthority, **extra: Any) -> dict[str, Any]:
    transaction = authority.spawn_store.load_attempt(authority.request["id"], 1)
    payload = {
        "status": status,
        "workflow_id": authority.request["workflow_id"],
        "request_id": authority.request["id"],
        "transaction_id": transaction["id"],
        "spawn_state": transaction["state"],
        "needs_rearm": transaction["state"] in _RECOVERABLE_TRANSACTION_STATES,
    }
    payload.update(extra)
    return payload


def _browser_failure_details(result: dict[str, Any]) -> dict[str, Any]:
    details: dict[str, Any] = {}
    route = result.get("route")
    if isinstance(route, str) and route:
        details["browser_route"] = route
    diagnostic = result.get("diagnostic")
    if isinstance(diagnostic, dict):
        details["browser_diagnostic"] = diagnostic
    return details


def run_live_slice(
    layout: DevLabLayout,
    *,
    launch_nonce: str,
    session_factory: Callable[[DevLabLayout], BrowserSession] | None = None,
    now: Callable[[], datetime] = _utc_now,
    login_timeout_seconds: int = DEFAULT_LOGIN_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    validate_dev_lab_layout(layout)
    if type(login_timeout_seconds) is not int or not 1 <= login_timeout_seconds <= MAX_BROWSER_TIMEOUT_SECONDS:
        raise ValueError(
            f"login timeout must be 1..{MAX_BROWSER_TIMEOUT_SECONDS} seconds"
        )

    recovered = _recover_registered_completion(layout, now=now())
    if recovered is not None:
        return {"status": "completed", "recovered": True, **recovered}

    authority = _consume_runner_arm(layout, launch_nonce=launch_nonce, now=now())
    journal = load_runner_journal(layout)
    if journal is None:
        journal = _write_journal(
            layout,
            document=authority.document,
            phase="arm_consumed",
            now=now(),
        )
    else:
        plan = authority.document["plan"]
        expected = (
            authority.document["plan_digest"],
            plan["request"]["workflow_id"],
            plan["request"]["child_request_id"],
            plan["spawn"]["transaction_id"],
        )
        actual = (
            journal["plan_digest"],
            journal["workflow_id"],
            journal["request_id"],
            journal["transaction_id"],
        )
        if actual != expected:
            raise RuntimeError("existing live runner journal belongs to different authority")

    session: BrowserSession | None = None
    try:
        transaction = authority.transaction
        if transaction["state"] in {
            "pending",
            "tab_created",
            "bootstrap_ready",
            "bootstrap_submitting",
        }:
            factory = session_factory or (lambda current_layout: SubprocessBrowserSession(current_layout))
            session = factory(layout)
            ready = _validate_result(
                session.wait_ready(timeout_seconds=login_timeout_seconds),
                action="wait_ready",
            )
            if not ready["ok"]:
                _write_journal(
                    layout,
                    document=authority.document,
                    phase="waiting_login",
                    now=now(),
                )
                return _result("paused", authority, reason=ready["reason"])

        if transaction["state"] == "pending":
            intent = _intent(authority)
            recover_only = journal["phase"] == "create_started"
            if not recover_only:
                journal = _write_journal(
                    layout,
                    document=authority.document,
                    phase="create_started",
                    now=now(),
                )
                create_result = _validate_result(session.create(intent), action="create")
            else:
                create_result = _validate_result(
                    session.recover_create(intent),
                    action="recover_create",
                )
            if not create_result["ok"]:
                reason = create_result["reason"]
                status = "manual_attach_required" if recover_only else "create_uncertain"
                return _result(status, authority, reason=reason)
            tab_id = create_result.get("tabId")
            if type(tab_id) is not int or tab_id < 1:
                raise RuntimeError("live browser create response is missing a positive tabId")
            authority.spawn_store.begin_browser_attempt(
                authority.request["id"],
                1,
                tab_id=tab_id,
                updated_at=_iso(now()),
            )
            authority = _refresh(authority)
            journal = _write_journal(
                layout,
                document=authority.document,
                phase="tab_persisted",
                now=now(),
            )

        if authority.transaction["state"] == "tab_created":
            authority.spawn_store.advance(
                authority.request["id"],
                1,
                target="bootstrap_ready",
                updated_at=_iso(now()),
            )
            authority = _refresh(authority)
            journal = _write_journal(
                layout,
                document=authority.document,
                phase="bootstrap_ready",
                now=now(),
            )

        if authority.transaction["state"] == "bootstrap_ready":
            browser_intent = _intent(authority)
            reattach_history = journal["phase"] in {
                "pre_submit_reattach_started",
                "pre_submit_reattached",
                "waiting_pre_submit_after_reattach",
            }
            if journal["phase"] in {
                "waiting_pre_submit",
                "pre_submit_reattach_started",
                "pre_submit_reattached",
                "waiting_pre_submit_after_reattach",
            }:
                recovered = _validate_result(
                    session.recover_create(browser_intent),
                    action="recover_create",
                )
                if (
                    not recovered["ok"]
                    and recovered["reason"] == "spawn_create_recovery_missing"
                    and journal["phase"] == "waiting_pre_submit"
                ):
                    journal = _write_journal(
                        layout,
                        document=authority.document,
                        phase="pre_submit_reattach_started",
                        now=now(),
                    )
                    recovered = _validate_result(
                        session.reattach_pre_submit(browser_intent),
                        action="reattach_pre_submit",
                    )
                    reattach_history = True
                if not recovered["ok"]:
                    return _result(
                        "manual_attach_required",
                        authority,
                        reason=recovered["reason"],
                    )
                recovered_tab_id = recovered.get("tabId")
                if type(recovered_tab_id) is not int or recovered_tab_id < 1:
                    raise RuntimeError(
                        "live browser pre-submit recovery is missing a positive tabId"
                    )
                browser_intent = {**browser_intent, "tab_id": recovered_tab_id}
                if reattach_history:
                    journal = _write_journal(
                        layout,
                        document=authority.document,
                        phase="pre_submit_reattached",
                        now=now(),
                    )
            probe = _validate_result(session.probe(browser_intent), action="probe")
            if not probe["ok"]:
                phase = (
                    "waiting_pre_submit_after_reattach"
                    if reattach_history
                    else "waiting_pre_submit"
                )
                _write_journal(
                    layout,
                    document=authority.document,
                    phase=phase,
                    now=now(),
                )
                status = "manual_attach_required" if reattach_history else "paused"
                return _result(status, authority, reason=probe["reason"])
            authority.spawn_store.advance(
                authority.request["id"],
                1,
                target="bootstrap_submitting",
                updated_at=_iso(now()),
            )
            authority = _refresh(authority)
            journal = _write_journal(
                layout,
                document=authority.document,
                phase="bootstrap_submitting",
                now=now(),
            )
            submit = _validate_result(session.submit(browser_intent), action="submit")
            if not submit["ok"]:
                failed = authority.spawn_store.fail(
                    authority.request["id"],
                    1,
                    reason=f"live submit unresolved: {submit['reason']}",
                    updated_at=_iso(now()),
                )
                authority = _refresh(authority)
                _write_journal(
                    layout,
                    document=authority.document,
                    phase="ambiguous",
                    now=now(),
                )
                return _result(
                    "manual_attach_required",
                    authority,
                    reason=failed["failure_reason"],
                    **_browser_failure_details(submit),
                )
            child_url = submit.get("childConversationUrl")
            if not isinstance(child_url, str):
                raise RuntimeError("live submit success is missing childConversationUrl")
            child_url = contract.canonical_conversation_url(child_url)
            authority.spawn_store.advance(
                authority.request["id"],
                1,
                target="identity_discovered",
                child_conversation_url=child_url,
                updated_at=_iso(now()),
            )
            authority = _refresh(authority)
            journal = _write_journal(
                layout,
                document=authority.document,
                phase="identity_persisted",
                now=now(),
            )

        elif authority.transaction["state"] == "bootstrap_submitting":
            reconciled = _validate_result(
                session.reconcile(_intent(authority)),
                action="reconcile",
            )
            if not reconciled["ok"]:
                failed = authority.spawn_store.fail(
                    authority.request["id"],
                    1,
                    reason=f"live submit recovery unresolved: {reconciled['reason']}",
                    updated_at=_iso(now()),
                )
                authority = _refresh(authority)
                _write_journal(
                    layout,
                    document=authority.document,
                    phase="ambiguous",
                    now=now(),
                )
                return _result(
                    "manual_attach_required",
                    authority,
                    reason=failed["failure_reason"],
                    **_browser_failure_details(reconciled),
                )
            child_url = reconciled.get("childConversationUrl")
            if not isinstance(child_url, str):
                raise RuntimeError("live reconcile success is missing childConversationUrl")
            child_url = contract.canonical_conversation_url(child_url)
            authority.spawn_store.advance(
                authority.request["id"],
                1,
                target="identity_discovered",
                child_conversation_url=child_url,
                updated_at=_iso(now()),
            )
            authority = _refresh(authority)
            journal = _write_journal(
                layout,
                document=authority.document,
                phase="identity_persisted",
                now=now(),
            )

        if authority.transaction["state"] == "identity_discovered":
            authority.spawn_store.advance(
                authority.request["id"],
                1,
                target="registration_submitting",
                updated_at=_iso(now()),
            )
            authority = _refresh(authority)
            journal = _write_journal(
                layout,
                document=authority.document,
                phase="registration_started",
                now=now(),
            )

        if authority.transaction["state"] == "registration_submitting":
            candidate = _registration(authority, registered_at=_iso(now()))
            authority.conversation_store.register_child(candidate)
            authority.spawn_store.advance(
                authority.request["id"],
                1,
                target="done",
                updated_at=_iso(now()),
            )
            authority = _refresh(authority)
            evidence = _write_completion_evidence(layout, authority, now=now())
            return {"status": "completed", "recovered": False, **evidence}

        raise RuntimeError(
            f"live runner reached unsupported nonterminal state {authority.transaction['state']!r}"
        )
    finally:
        if session is not None:
            session.close()


class SubprocessBrowserSession:
    def __init__(self, layout: DevLabLayout) -> None:
        validate_dev_lab_layout(layout)
        node = shutil.which("node")
        if node is None:
            raise RuntimeError("Node.js is required for the DEV live browser actuator")
        checkout = _resolved(layout.checkout)
        script = _resolved(checkout / "scripts" / "conversation_live_slice_browser.cjs")
        extension = _resolved(checkout / "chat_bridge")
        if checkout not in script.parents or script.is_symlink() or not script.is_file():
            raise RuntimeError(f"live browser actuator script is unavailable or unsafe: {script}")
        if checkout not in extension.parents or extension.is_symlink() or not extension.is_dir():
            raise RuntimeError(f"DEV Chat Bridge extension is unavailable or unsafe: {extension}")
        profile = live_slice_paths(layout).browser_profile
        profile.mkdir(parents=True, exist_ok=True)
        self._next_id = 0
        self._process = subprocess.Popen(
            [
                node,
                str(script),
                "--profile",
                str(profile),
                "--extension",
                str(extension),
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=None,
            text=True,
            encoding="utf-8",
            bufsize=1,
            env=_browser_environment(layout),
        )

    def _request(
        self,
        action: str,
        payload: dict[str, Any] | None = None,
        *,
        timeout_seconds: int = DEFAULT_BROWSER_TIMEOUT_SECONDS,
    ) -> dict[str, Any]:
        if type(timeout_seconds) is not int or not 1 <= timeout_seconds <= MAX_BROWSER_TIMEOUT_SECONDS:
            raise ValueError(
                f"browser timeout must be 1..{MAX_BROWSER_TIMEOUT_SECONDS} seconds"
            )
        if self._process.poll() is not None:
            raise RuntimeError(
                "DEV live browser actuator exited; install Playwright/Chromium if stderr reports a missing dependency"
            )
        if self._process.stdin is None or self._process.stdout is None:
            raise RuntimeError("DEV live browser actuator pipes are unavailable")
        self._next_id += 1
        request_id = self._next_id
        line = json.dumps(
            {"id": request_id, "action": action, "payload": payload or {}},
            separators=(",", ":"),
        )
        if len(line) > MAX_PROTOCOL_LINE_CHARS:
            raise RuntimeError("DEV live browser request exceeds protocol bound")
        self._process.stdin.write(line + "\n")
        self._process.stdin.flush()

        ready, _, _ = select.select(
            [self._process.stdout],
            [],
            [],
            timeout_seconds,
        )
        if not ready:
            raise RuntimeError(f"DEV live browser action {action!r} timed out")
        response_line = self._process.stdout.readline()
        if not response_line:
            raise RuntimeError(f"DEV live browser action {action!r} lost its response channel")
        if len(response_line) > MAX_PROTOCOL_LINE_CHARS:
            raise RuntimeError("DEV live browser response exceeds protocol bound")
        try:
            response = json.loads(response_line)
        except json.JSONDecodeError as exc:
            raise RuntimeError("DEV live browser response is invalid JSON") from exc
        if not isinstance(response, dict) or response.get("id") != request_id:
            raise RuntimeError("DEV live browser response identity mismatch")
        if response.get("ok") is not True:
            raise RuntimeError(str(response.get("error") or f"browser action {action} failed"))
        result = response.get("result")
        if not isinstance(result, dict):
            raise RuntimeError("DEV live browser response result must be an object")
        return result

    def wait_ready(self, *, timeout_seconds: int) -> dict[str, Any]:
        response_timeout = min(MAX_BROWSER_TIMEOUT_SECONDS, timeout_seconds + 5)
        return self._request(
            "wait_ready",
            {"timeout_ms": timeout_seconds * 1000},
            timeout_seconds=response_timeout,
        )

    def create(self, intent: dict[str, Any]) -> dict[str, Any]:
        return self._request("create", {"intent": intent})

    def recover_create(self, intent: dict[str, Any]) -> dict[str, Any]:
        return self._request("recover_create", {"intent": intent})

    def reattach_pre_submit(self, intent: dict[str, Any]) -> dict[str, Any]:
        return self._request("reattach_pre_submit", {"intent": intent})

    def probe(self, intent: dict[str, Any]) -> dict[str, Any]:
        return self._request("probe", {"intent": intent})

    def submit(self, intent: dict[str, Any]) -> dict[str, Any]:
        return self._request("submit", {"intent": intent})

    def reconcile(self, intent: dict[str, Any]) -> dict[str, Any]:
        return self._request("reconcile", {"intent": intent})

    @staticmethod
    def _child_payload(
        child_conversation_url: str,
        ownership: dict[str, Any] | None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"child_conversation_url": child_conversation_url}
        if ownership is not None:
            payload["ownership"] = dict(ownership)
        return payload

    def open_child(
        self,
        child_conversation_url: str,
        ownership: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return self._request("open_child", self._child_payload(child_conversation_url, ownership))

    def observe_child(
        self,
        child_conversation_url: str,
        ownership: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return self._request(
            "observe_child",
            self._child_payload(child_conversation_url, ownership),
            timeout_seconds=CHILD_OBSERVE_BROWSER_TIMEOUT_SECONDS,
        )

    def close_child(
        self,
        child_conversation_url: str,
        ownership: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return self._request("close_child", self._child_payload(child_conversation_url, ownership))

    def close(self) -> None:
        if self._process.poll() is not None:
            return
        try:
            self._request("shutdown", timeout_seconds=10)
        except (OSError, RuntimeError, ValueError):
            self._process.terminate()
        try:
            self._process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self._process.kill()
            self._process.wait(timeout=5)


def _probe_live_slice_browser_login(
    layout: DevLabLayout,
    *,
    timeout_seconds: int,
    session_factory: Callable[[DevLabLayout], BrowserSession] | None = None,
) -> dict[str, Any]:
    factory = session_factory or (lambda current_layout: SubprocessBrowserSession(current_layout))
    session = factory(layout)
    try:
        return _validate_result(
            session.wait_ready(timeout_seconds=timeout_seconds),
            action="wait_ready",
        )
    finally:
        session.close()


def open_manual_login_browser(layout: DevLabLayout) -> dict[str, Any]:
    """Open normal Chrome on the persistent isolated DEV profile and wait for close."""
    validate_dev_lab_layout(layout)
    if sys.platform != "darwin":
        raise RuntimeError(
            "manual DEV ChatGPT login currently requires macOS Google Chrome"
        )
    opener = shutil.which("open")
    if opener is None:
        raise RuntimeError("macOS open command is required for manual DEV ChatGPT login")
    profile = _require_descendant(
        live_slice_paths(layout).browser_profile,
        layout.root,
        field="browser profile",
    )
    profile.mkdir(parents=True, exist_ok=True)
    print(
        "DEV live slice: sign in once in the normal Google Chrome window, "
        "then close that isolated Chrome window.",
        file=sys.stderr,
    )
    completed = subprocess.run(
        [
            opener,
            "-W",
            "-n",
            "-a",
            "Google Chrome",
            "--args",
            f"--user-data-dir={profile}",
            "--no-first-run",
            "--disable-background-mode",
            "--password-store=basic",
            "--use-mock-keychain",
            "--new-window",
            "https://chatgpt.com/",
        ],
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            f"normal Chrome manual login exited with status {completed.returncode}"
        )
    return {"ok": True, "reason": "manual_login_browser_closed"}


def login_live_slice_browser(
    layout: DevLabLayout,
    *,
    timeout_seconds: int = DEFAULT_LOGIN_TIMEOUT_SECONDS,
    session_factory: Callable[[DevLabLayout], BrowserSession] | None = None,
    manual_login_factory: Callable[[DevLabLayout], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Ensure the persistent DEV profile has a ChatGPT session.

    Existing sessions are reused. If the isolated profile is logged out, authentication
    is performed once in normal Chrome rather than in the Playwright-controlled browser.
    """
    validate_dev_lab_layout(layout)
    if type(timeout_seconds) is not int or not 1 <= timeout_seconds <= MAX_BROWSER_TIMEOUT_SECONDS:
        raise ValueError(
            f"login timeout must be 1..{MAX_BROWSER_TIMEOUT_SECONDS} seconds"
        )

    probe_timeout = min(timeout_seconds, DEFAULT_LOGIN_PROBE_TIMEOUT_SECONDS)
    initial = _probe_live_slice_browser_login(
        layout,
        timeout_seconds=probe_timeout,
        session_factory=session_factory,
    )
    if initial.get("ok") is True:
        return {**initial, "manual_login_used": False}
    if initial.get("reason") != "chatgpt_login_timeout":
        return {**initial, "manual_login_used": False}

    launcher = manual_login_factory or open_manual_login_browser
    manual = _validate_result(launcher(layout), action="manual_login")
    if not manual["ok"]:
        return {**manual, "manual_login_used": True}

    verify_timeout = min(timeout_seconds, DEFAULT_LOGIN_VERIFY_TIMEOUT_SECONDS)
    verified = _probe_live_slice_browser_login(
        layout,
        timeout_seconds=verify_timeout,
        session_factory=session_factory,
    )
    return {**verified, "manual_login_used": True}


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
    parser.add_argument("command", choices=("login", "run"))
    parser.add_argument("--launch-nonce")
    parser.add_argument(
        "--login-timeout-seconds",
        type=int,
        default=DEFAULT_LOGIN_TIMEOUT_SECONDS,
    )
    parser.add_argument("--home")
    parser.add_argument("--root")
    parser.add_argument("--checkout")
    parser.add_argument("--production-checkout")
    return parser.parse_args(list(argv) if argv is not None else None)


def main(argv: Iterable[str] | None = None) -> int:
    try:
        args = parse_args(argv)
        layout = _layout_from_args(args)
        if args.command == "login":
            payload = login_live_slice_browser(
                layout,
                timeout_seconds=args.login_timeout_seconds,
            )
            return_code = 0 if payload["ok"] else 3
        else:
            if not args.launch_nonce:
                raise ValueError("--launch-nonce is required for run")
            payload = run_live_slice(
                layout,
                launch_nonce=str(args.launch_nonce),
                login_timeout_seconds=args.login_timeout_seconds,
            )
            return_code = 0 if payload["status"] == "completed" else 3
        print(_json_text(payload), end="")
        return return_code
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"DEV live runner error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
