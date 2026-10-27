"""Bounded DEV MVP for real Conversation Fabric child delegation.

One explicit invocation authors up to four reasoning children, spawns them through the
existing isolated browser actuator, observes bounded final assistant results, records
terminal/adoption/retirement durability, and closes only retired child tabs.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from collections.abc import Callable, Iterable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from local_agent.conversation import adoption, contract, terminal
from local_agent.conversation.spawn_store import WorkflowConversationSpawnStore
from local_agent.conversation.store import WorkflowConversationStore
from local_agent.development.lab import DevLabLayout, build_dev_lab_layout, validate_dev_lab_layout
from local_agent.development.live_runner import (
    DEFAULT_LOGIN_TIMEOUT_SECONDS,
    SubprocessBrowserSession,
    run_live_slice,
)
from local_agent.development.live_seed import CheckoutIdentity, inspect_live_seed_checkout
from local_agent.development.live_slice import arm_live_slice, prepare_live_slice
from local_agent.foundation.process import atomic_write_text, fsync_directory
from local_agent.workflow import contract as workflow_contract
from local_agent.workflow import publishing
from local_agent.workflow.store import WorkflowStore

MVP_SCHEMA_VERSION = 1
MAX_MVP_CHILDREN = 4
MAX_RESULT_CHARS = 16_384
MAX_RESULT_TIMEOUT_SECONDS = 30 * 60
DEFAULT_RESULT_TIMEOUT_SECONDS = 10 * 60
DEFAULT_RESULT_POLL_SECONDS = 1.0
RESULT_STABLE_OBSERVATIONS = 3


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _canonical_bytes(payload: Any) -> bytes:
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _sha256_payload(payload: Any) -> str:
    return "sha256:" + hashlib.sha256(_canonical_bytes(payload)).hexdigest()


def _load_spec(path: Path) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"MVP spec is unavailable or unsafe: {path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"MVP spec is invalid: {path}") from exc
    if not isinstance(payload, dict):
        raise ValueError("MVP spec must be an object")
    return payload


def validate_mvp_spec(spec: dict[str, Any]) -> None:
    required = {"schema_version", "workflow_id", "parent_conversation_url", "children"}
    if not isinstance(spec, dict) or set(spec) != required:
        raise ValueError("MVP spec fields do not match schema")
    if spec.get("schema_version") != MVP_SCHEMA_VERSION:
        raise ValueError(f"MVP spec schema_version must be {MVP_SCHEMA_VERSION}")
    if not isinstance(spec.get("workflow_id"), str) or not spec["workflow_id"]:
        raise ValueError("MVP workflow_id must be a non-empty string")
    parent = contract.canonical_conversation_url(spec.get("parent_conversation_url"))
    if parent != spec["parent_conversation_url"]:
        raise ValueError("MVP parent_conversation_url must be canonical")
    children = spec.get("children")
    if not isinstance(children, list) or not 1 <= len(children) <= MAX_MVP_CHILDREN:
        raise ValueError(f"MVP children must contain 1..{MAX_MVP_CHILDREN} items")
    seen_requests: set[str] = set()
    seen_nodes: set[str] = set()
    child_fields = {"request_id", "node_id", "role", "summary", "paths"}
    for child in children:
        if not isinstance(child, dict) or set(child) != child_fields:
            raise ValueError("MVP child fields do not match schema")
        request_id = child.get("request_id")
        node_id = child.get("node_id")
        if not isinstance(request_id, str) or not request_id:
            raise ValueError("MVP child request_id must be non-empty")
        if not isinstance(node_id, str) or not node_id:
            raise ValueError("MVP child node_id must be non-empty")
        if request_id in seen_requests or node_id in seen_nodes:
            raise ValueError("MVP child request_id and node_id values must be unique")
        seen_requests.add(request_id)
        seen_nodes.add(node_id)
        if child.get("role") not in contract.CHILD_ROLES:
            raise ValueError(f"MVP child role must be one of {sorted(contract.CHILD_ROLES)!r}")
        summary = child.get("summary")
        if not isinstance(summary, str) or not summary.strip():
            raise ValueError("MVP child summary must be non-empty")
        paths = child.get("paths")
        if not isinstance(paths, list) or not paths or not all(
            isinstance(item, str) and item.strip() for item in paths
        ):
            raise ValueError("MVP child paths must be a non-empty string list")


def _manifest(spec: dict[str, Any], identity: CheckoutIdentity, *, created_at: str) -> dict[str, Any]:
    manifest = {
        "schema_version": workflow_contract.WORKFLOW_SCHEMA_VERSION,
        "id": spec["workflow_id"],
        "created_at": created_at,
        "nodes": [
            {
                "id": child["node_id"],
                "kind": "reasoning",
                "repository_id": identity.repository_id,
                "agent_binding": identity.agent_binding,
                "depends_on": [],
            }
            for child in spec["children"]
        ],
    }
    workflow_contract.validate_workflow_manifest(manifest)
    return manifest


def _request(
    spec: dict[str, Any],
    child: dict[str, Any],
    manifest: dict[str, Any],
    identity: CheckoutIdentity,
    *,
    created_at: str,
) -> dict[str, Any]:
    revision, introduction_digest = publishing.node_introduction_identity(
        manifest, [], child["node_id"]
    )
    request = {
        "schema_version": contract.CHILD_REQUEST_SCHEMA_VERSION,
        "id": child["request_id"],
        "workflow_id": manifest["id"],
        "workflow_node_id": child["node_id"],
        "workflow_node_revision": revision,
        "workflow_node_introduction_digest": introduction_digest,
        "parent_conversation_url": spec["parent_conversation_url"],
        "created_at": created_at,
        "role": child["role"],
        "repository_id": identity.repository_id,
        "agent_binding": identity.agent_binding,
        "repository_ref": identity.repository_ref,
        "repository_commit_sha": identity.repository_commit_sha,
        "scope": {"summary": child["summary"].strip(), "paths": list(child["paths"])},
        "context_refs": [],
        "bootstrap_contract_version": contract.BOOTSTRAP_CONTRACT_VERSION,
    }
    contract.validate_child_request(request)
    return request


def _seed_campaign(
    layout: DevLabLayout,
    spec: dict[str, Any],
    *,
    now: datetime,
    identity_provider: Callable[[DevLabLayout], CheckoutIdentity],
) -> tuple[WorkflowStore, WorkflowConversationStore, list[dict[str, Any]]]:
    identity = identity_provider(layout)
    workflow_store = WorkflowStore(layout.state_dir)
    workflow_id = str(spec["workflow_id"])
    if workflow_id in workflow_store.workflow_ids():
        manifest = workflow_store.load_manifest(workflow_id)
        expected = _manifest(spec, identity, created_at=str(manifest.get("created_at", "")))
        if workflow_contract.manifest_digest(manifest) != workflow_contract.manifest_digest(expected):
            raise RuntimeError("existing MVP workflow conflicts with current spec or checkout identity")
    else:
        manifest = _manifest(spec, identity, created_at=_iso(now))
        workflow_store.submit(manifest)

    conversations = WorkflowConversationStore(workflow_store, workflow_id)
    requests: list[dict[str, Any]] = []
    for child in spec["children"]:
        existing = conversations.request_for_node(child["node_id"])
        if existing is None:
            candidate = _request(spec, child, manifest, identity, created_at=_iso(now))
            conversations.admit_request(candidate)
            existing = conversations.load_request(child["request_id"])
        else:
            expected = _request(
                spec, child, manifest, identity, created_at=str(existing.get("created_at", ""))
            )
            if existing.get("id") != child["request_id"] or (
                contract.child_request_digest(existing) != contract.child_request_digest(expected)
            ):
                raise RuntimeError("existing MVP child request conflicts with current spec")
        requests.append(existing)
    return workflow_store, conversations, requests


def _automatic_spawn(
    layout: DevLabLayout,
    workflow_id: str,
    request_id: str,
    *,
    login_timeout_seconds: int,
) -> dict[str, Any]:
    conversations = WorkflowConversationStore(WorkflowStore(layout.state_dir), workflow_id)
    registration = conversations.load_registration(request_id)
    if registration is not None:
        return {
            "status": "completed",
            "recovered": True,
            "child_conversation_url": registration["child_conversation_url"],
        }
    document = prepare_live_slice(layout, workflow_id, request_id)
    arm = arm_live_slice(layout, expected_plan_digest=str(document["plan_digest"]))
    result = run_live_slice(
        layout,
        launch_nonce=str(arm["launch_nonce"]),
        login_timeout_seconds=login_timeout_seconds,
    )
    if result.get("status") != "completed":
        raise RuntimeError(f"MVP automatic spawn did not complete: {result!r}")
    return result


def _result_root(layout: DevLabLayout, workflow_id: str) -> Path:
    return layout.state_dir / "conversation-mvp" / workflow_id / "results"


_RESULT_FIELDS = frozenset(
    {
        "schema_version",
        "workflow_id",
        "request_id",
        "request_digest",
        "child_conversation_url",
        "assistant_identity",
        "assistant_text",
        "truncated",
        "captured_at",
    }
)


def _result_path(layout: DevLabLayout, request: dict[str, Any]) -> Path:
    return _result_root(layout, request["workflow_id"]) / f"{request['id']}.json"


def _result_payload(
    request: dict[str, Any],
    registration: dict[str, Any],
    observed: dict[str, Any],
    *,
    captured_at: str,
) -> dict[str, Any]:
    text = str(observed.get("assistant_text") or "").strip()
    if not text:
        raise RuntimeError("MVP assistant result is empty")
    if len(text) > MAX_RESULT_CHARS:
        raise RuntimeError("MVP assistant result exceeded the browser bound")
    identity = str(observed.get("assistant_identity") or "").strip()
    if not identity:
        raise RuntimeError("MVP assistant result identity is missing")
    return {
        "schema_version": MVP_SCHEMA_VERSION,
        "workflow_id": request["workflow_id"],
        "request_id": request["id"],
        "request_digest": contract.child_request_digest(request),
        "child_conversation_url": registration["child_conversation_url"],
        "assistant_identity": identity,
        "assistant_text": text,
        "truncated": bool(observed.get("truncated")),
        "captured_at": captured_at,
    }


def _validate_result_payload(
    payload: dict[str, Any],
    request: dict[str, Any],
    registration: dict[str, Any],
) -> None:
    if not isinstance(payload, dict) or set(payload) != _RESULT_FIELDS:
        raise RuntimeError("MVP durable result fields do not match schema")
    if payload.get("schema_version") != MVP_SCHEMA_VERSION:
        raise RuntimeError("MVP durable result schema version mismatch")
    expected = {
        "workflow_id": request["workflow_id"],
        "request_id": request["id"],
        "request_digest": contract.child_request_digest(request),
        "child_conversation_url": registration["child_conversation_url"],
    }
    for field, value in expected.items():
        if payload.get(field) != value:
            raise RuntimeError(f"MVP durable result {field} conflicts with admitted authority")
    if not isinstance(payload.get("assistant_identity"), str) or not payload["assistant_identity"].strip():
        raise RuntimeError("MVP durable result assistant identity is invalid")
    text = payload.get("assistant_text")
    if not isinstance(text, str) or not text.strip() or len(text) > MAX_RESULT_CHARS:
        raise RuntimeError("MVP durable result assistant text is invalid")
    if type(payload.get("truncated")) is not bool:
        raise RuntimeError("MVP durable result truncated flag is invalid")
    captured_at = payload.get("captured_at")
    if not isinstance(captured_at, str) or not captured_at.endswith("Z") or len(captured_at) > 64:
        raise RuntimeError("MVP durable result captured_at is invalid")
    try:
        datetime.fromisoformat(captured_at[:-1] + "+00:00")
    except ValueError as exc:
        raise RuntimeError("MVP durable result captured_at is invalid") from exc


def _read_result_payload(
    layout: DevLabLayout,
    request: dict[str, Any],
    registration: dict[str, Any],
) -> tuple[dict[str, Any], str]:
    path = _result_path(layout, request)
    try:
        if path.is_symlink() or not path.is_file():
            raise OSError("result path is not a regular file")
        raw = path.read_bytes()
    except OSError as exc:
        raise RuntimeError("MVP durable result evidence is unavailable") from exc
    if len(raw) > (MAX_RESULT_CHARS * 4) + 8192:
        raise RuntimeError("MVP durable result evidence exceeds bounds")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("MVP durable result evidence is invalid") from exc
    _validate_result_payload(payload, request, registration)
    return payload, _sha256_payload(payload)


def _same_result_semantics(left: dict[str, Any], right: dict[str, Any]) -> bool:
    return all(
        left.get(field) == right.get(field)
        for field in _RESULT_FIELDS
        if field != "captured_at"
    )


def _persist_observation(
    layout: DevLabLayout,
    request: dict[str, Any],
    registration: dict[str, Any],
    observed: dict[str, Any],
    *,
    captured_at: str,
) -> tuple[dict[str, Any], str]:
    candidate = _result_payload(
        request,
        registration,
        observed,
        captured_at=captured_at,
    )
    root = _result_root(layout, request["workflow_id"])
    root.mkdir(parents=True, exist_ok=True)
    path = _result_path(layout, request)
    workflow_store = WorkflowStore(layout.state_dir)
    with workflow_store.execution_lock(request["workflow_id"]):
        if path.exists() or path.is_symlink():
            existing, digest = _read_result_payload(layout, request, registration)
            if not _same_result_semantics(existing, candidate):
                raise RuntimeError("MVP durable result evidence conflicts with new observation")
            return existing, digest
        atomic_write_text(
            path,
            json.dumps(candidate, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
        )
        fsync_directory(root)
    return candidate, _sha256_payload(candidate)


def _terminal_result_digest(record: dict[str, Any], request_id: str) -> str:
    refs = [
        item
        for item in record.get("evidence_refs", [])
        if isinstance(item, dict)
        and item.get("kind") == "child-result"
        and item.get("id") == f"{request_id}.result"
    ]
    if len(refs) != 1 or not isinstance(refs[0].get("digest"), str):
        raise RuntimeError("MVP terminal record does not bind exactly one child result")
    return str(refs[0]["digest"])


def _record_completed_result(
    layout: DevLabLayout,
    conversations: WorkflowConversationStore,
    request: dict[str, Any],
    observed: dict[str, Any] | None,
    *,
    now: datetime,
) -> dict[str, Any]:
    registration = conversations.load_registration(request["id"])
    if registration is None:
        raise RuntimeError("MVP result requires durable child registration")
    lifecycle = conversations.load_state(request["id"])["state"]
    path = _result_path(layout, request)

    if lifecycle in {"terminal_recorded", "retired"} and not path.is_file():
        raise RuntimeError("MVP terminal state is missing durable child result evidence")

    if path.is_file():
        evidence, evidence_digest = _read_result_payload(layout, request, registration)
        if observed is not None:
            candidate = _result_payload(
                request,
                registration,
                observed,
                captured_at=evidence["captured_at"],
            )
            if not _same_result_semantics(evidence, candidate):
                raise RuntimeError("MVP browser observation conflicts with durable child result")
    else:
        if observed is None:
            raise RuntimeError("MVP result recovery requires existing durable evidence")
        evidence, evidence_digest = _persist_observation(
            layout,
            request,
            registration,
            observed,
            captured_at=_iso(now),
        )

    terminal_record = conversations.load_terminal_record(request["id"])
    if lifecycle == "retired":
        if terminal_record is None:
            raise RuntimeError("MVP retired child is missing terminal record")
        if _terminal_result_digest(terminal_record, request["id"]) != evidence_digest:
            raise RuntimeError("MVP retired child result digest conflicts with terminal record")
        return {
            "request_id": request["id"],
            "request_digest": contract.child_request_digest(request),
            "child_conversation_url": registration["child_conversation_url"],
            "child_state": "retired",
            "assistant_identity": evidence["assistant_identity"],
            "summary": terminal_record["summary"],
            "evidence_digest": evidence_digest,
        }

    if lifecycle == "active":
        conversations.transition_state(request["id"], "terminal_pending_evidence")
        lifecycle = "terminal_pending_evidence"

    if lifecycle == "terminal_pending_evidence":
        summary = evidence["assistant_text"][: terminal.MAX_TERMINAL_SUMMARY_CHARS].strip()
        conversations.record_terminal(
            {
                "schema_version": terminal.TERMINAL_RECORD_SCHEMA_VERSION,
                "child_request_id": request["id"],
                "child_request_digest": contract.child_request_digest(request),
                "child_registration_digest": terminal.child_registration_digest(
                    registration, request=request
                ),
                "child_conversation_url": registration["child_conversation_url"],
                "summary": summary,
                "evidence_refs": [
                    {
                        "kind": "child-result",
                        "id": f"{request['id']}.result",
                        "digest": evidence_digest,
                    }
                ],
                "recorded_at": evidence["captured_at"],
            }
        )
    elif lifecycle != "terminal_recorded":
        raise RuntimeError(f"MVP cannot finalize child lifecycle state {lifecycle!r}")

    terminal_record = conversations.load_terminal_record(request["id"])
    if terminal_record is None:
        raise RuntimeError("MVP terminal record is missing after result capture")
    if _terminal_result_digest(terminal_record, request["id"]) != evidence_digest:
        raise RuntimeError("MVP durable result digest conflicts with terminal record")

    conversations.record_adoption(
        {
            "schema_version": adoption.ADOPTION_RECORD_SCHEMA_VERSION,
            "child_request_id": request["id"],
            "child_request_digest": contract.child_request_digest(request),
            "terminal_record_digest": adoption.terminal_record_digest(
                terminal_record, request=request, registration=registration
            ),
            "workflow_id": request["workflow_id"],
            "workflow_node_id": request["workflow_node_id"],
            "adopted_at": evidence["captured_at"],
        }
    )
    if conversations.load_state(request["id"])["state"] != "retired":
        conversations.transition_state(request["id"], "retired")
    return {
        "request_id": request["id"],
        "request_digest": contract.child_request_digest(request),
        "child_conversation_url": registration["child_conversation_url"],
        "child_state": conversations.load_state(request["id"])["state"],
        "assistant_identity": evidence["assistant_identity"],
        "summary": terminal_record["summary"],
        "evidence_digest": evidence_digest,
    }

def _require_browser_result(result: Any, *, action: str) -> dict[str, Any]:
    if not isinstance(result, dict) or type(result.get("ok")) is not bool:
        raise RuntimeError(f"MVP browser {action} returned invalid result")
    if not isinstance(result.get("reason"), str) or not result["reason"]:
        raise RuntimeError(f"MVP browser {action} result is missing reason")
    if not result["ok"]:
        raise RuntimeError(f"MVP browser {action} failed closed: {result!r}")
    return result


def _child_browser_ownership(
    conversations: WorkflowConversationStore,
    request_id: str,
) -> dict[str, Any] | None:
    attempts = WorkflowConversationSpawnStore(conversations).load_attempts(request_id)
    if len(attempts) != 1:
        return None
    attempt = attempts[0]
    return {
        "transaction_id": attempt["id"],
        "child_request_digest": attempt["child_request_digest"],
        "bootstrap_digest": attempt["bootstrap_digest"],
    }


def _failure_outcome(
    conversations: WorkflowConversationStore,
    request: dict[str, Any],
    message: str,
) -> dict[str, Any]:
    registration = conversations.load_registration(request["id"])
    return {
        "request_id": request["id"],
        "request_digest": contract.child_request_digest(request),
        "child_conversation_url": (
            registration["child_conversation_url"] if registration is not None else None
        ),
        "child_state": conversations.load_state(request["id"])["state"],
        "error": message,
    }


def _append_failure(failures: dict[str, str], request_id: str, message: str) -> None:
    existing = failures.get(request_id)
    failures[request_id] = message if existing is None else f"{existing}; {message}"


def run_mvp_campaign(
    layout: DevLabLayout,
    spec: dict[str, Any],
    *,
    login_timeout_seconds: int = DEFAULT_LOGIN_TIMEOUT_SECONDS,
    result_timeout_seconds: int = DEFAULT_RESULT_TIMEOUT_SECONDS,
    poll_seconds: float = DEFAULT_RESULT_POLL_SECONDS,
    identity_provider: Callable[[DevLabLayout], CheckoutIdentity] = inspect_live_seed_checkout,
    spawn_runner: Callable[..., dict[str, Any]] = _automatic_spawn,
    result_session_factory: Callable[[DevLabLayout], Any] = SubprocessBrowserSession,
    now: Callable[[], datetime] = _utc_now,
) -> dict[str, Any]:
    validate_dev_lab_layout(layout)
    validate_mvp_spec(spec)
    if type(result_timeout_seconds) is not int or not 1 <= result_timeout_seconds <= MAX_RESULT_TIMEOUT_SECONDS:
        raise ValueError("MVP result timeout is out of bounds")
    if not isinstance(poll_seconds, (int, float)) or not 0 <= poll_seconds <= 5:
        raise ValueError("MVP poll_seconds must be 0..5")
    workflow_store, conversations, requests = _seed_campaign(
        layout, spec, now=now(), identity_provider=identity_provider
    )

    outcomes: dict[str, dict[str, Any]] = {}
    failures: dict[str, str] = {}
    spawn_blocked = False
    for request in requests:
        request_id = request["id"]
        lifecycle = conversations.load_state(request_id)["state"]
        if lifecycle == "retired" or conversations.load_registration(request_id) is not None:
            continue
        if lifecycle == "abandoned":
            failures[request_id] = "child was explicitly abandoned after unresolved ambiguous spawn"
            continue
        if spawn_blocked:
            failures[request_id] = "spawn skipped after an earlier unresolved spawn failure"
            continue
        try:
            spawn_runner(
                layout,
                request["workflow_id"],
                request_id,
                login_timeout_seconds=login_timeout_seconds,
            )
        except (OSError, RuntimeError, ValueError) as exc:
            failures[request_id] = f"spawn failed: {exc}"
            spawn_blocked = True
            continue
        if conversations.load_registration(request_id) is None:
            failures[request_id] = "spawn completed without durable registration"
            spawn_blocked = True

    # Complete crash-recoverable lifecycle work from durable evidence before touching the browser.
    for request in requests:
        request_id = request["id"]
        lifecycle = conversations.load_state(request_id)["state"]
        registration = conversations.load_registration(request_id)
        if registration is None:
            continue
        if lifecycle == "retired":
            try:
                outcomes[request_id] = _record_completed_result(
                    layout, conversations, request, None, now=now()
                )
            except (OSError, RuntimeError, ValueError) as exc:
                _append_failure(failures, request_id, f"retired recovery failed: {exc}")
        elif lifecycle in {"terminal_pending_evidence", "terminal_recorded"} and _result_path(
            layout, request
        ).is_file():
            try:
                outcomes[request_id] = _record_completed_result(
                    layout, conversations, request, None, now=now()
                )
                failures.pop(request_id, None)
            except (OSError, RuntimeError, ValueError) as exc:
                _append_failure(failures, request_id, f"durable result recovery failed: {exc}")
        elif lifecycle == "terminal_recorded":
            _append_failure(
                failures,
                request_id,
                "terminal_recorded child is missing durable result evidence",
            )

    pending: dict[str, dict[str, Any]] = {}
    for request in requests:
        request_id = request["id"]
        if request_id in outcomes:
            continue
        lifecycle = conversations.load_state(request_id)["state"]
        registration = conversations.load_registration(request_id)
        if lifecycle == "retired":
            continue
        if registration is not None:
            pending[request_id] = request
        else:
            failures.setdefault(request_id, "child has no durable registration")

    cleanup_urls: dict[str, tuple[str, dict[str, Any] | None]] = {}
    for request in requests:
        if conversations.load_state(request["id"])["state"] != "retired":
            continue
        registration = conversations.load_registration(request["id"])
        if registration is not None:
            cleanup_urls[request["id"]] = (
                str(registration["child_conversation_url"]),
                _child_browser_ownership(conversations, request["id"]),
            )

    if pending or cleanup_urls:
        session = result_session_factory(layout)
        try:
            try:
                _require_browser_result(
                    session.wait_ready(timeout_seconds=login_timeout_seconds), action="wait_ready"
                )
            except (OSError, RuntimeError, ValueError) as exc:
                for request_id in pending:
                    _append_failure(failures, request_id, f"browser readiness failed: {exc}")
                for request_id in cleanup_urls:
                    _append_failure(failures, request_id, f"retired cleanup deferred: {exc}")
                pending.clear()
            else:
                for request_id, (child_url, ownership) in cleanup_urls.items():
                    try:
                        _require_browser_result(
                            session.close_child(child_url, ownership), action="close_child"
                        )
                    except (OSError, RuntimeError, ValueError) as exc:
                        _append_failure(failures, request_id, f"retired cleanup failed: {exc}")

                urls: dict[str, str] = {}
                ownerships: dict[str, dict[str, Any] | None] = {}
                deadlines: dict[str, float] = {}
                opened: dict[str, dict[str, Any]] = {}
                for request_id, request in pending.items():
                    registration = conversations.load_registration(request_id)
                    if registration is None:
                        _append_failure(failures, request_id, "pending result lost durable registration")
                        continue
                    child_url = str(registration["child_conversation_url"])
                    ownership = _child_browser_ownership(conversations, request_id)
                    try:
                        _require_browser_result(
                            session.open_child(child_url, ownership), action="open_child"
                        )
                    except (OSError, RuntimeError, ValueError) as exc:
                        _append_failure(failures, request_id, f"open child failed: {exc}")
                        continue
                    urls[request_id] = child_url
                    ownerships[request_id] = ownership
                    deadlines[request_id] = time.monotonic() + result_timeout_seconds
                    opened[request_id] = request
                pending = opened

                stable: dict[str, tuple[str, int]] = {}
                observe_retries: dict[str, int] = {}
                while pending:
                    completed: list[str] = []
                    for request_id, request in list(pending.items()):
                        if time.monotonic() >= deadlines[request_id]:
                            _append_failure(
                                failures,
                                request_id,
                                "timed out waiting for stable child result",
                            )
                            completed.append(request_id)
                            continue
                        try:
                            observed = _require_browser_result(
                                session.observe_child(
                                    urls[request_id], ownerships[request_id]
                                ),
                                action="observe_child",
                            )
                        except (OSError, RuntimeError, ValueError) as exc:
                            retries = observe_retries.get(request_id, 0)
                            if retries < 1 and time.monotonic() < deadlines[request_id]:
                                observe_retries[request_id] = retries + 1
                                stable.pop(request_id, None)
                                try:
                                    session.close()
                                except (OSError, RuntimeError, ValueError):
                                    pass
                                try:
                                    session = result_session_factory(layout)
                                    _require_browser_result(
                                        session.wait_ready(timeout_seconds=login_timeout_seconds),
                                        action="wait_ready",
                                    )
                                    _require_browser_result(
                                        session.open_child(
                                            urls[request_id], ownerships[request_id]
                                        ),
                                        action="open_child",
                                    )
                                except (OSError, RuntimeError, ValueError) as retry_exc:
                                    _append_failure(
                                        failures,
                                        request_id,
                                        f"observe child retry failed: {retry_exc}",
                                    )
                                    completed.append(request_id)
                                continue
                            _append_failure(failures, request_id, f"observe child failed: {exc}")
                            completed.append(request_id)
                            continue
                        if observed["reason"] != "child_result_ready":
                            stable.pop(request_id, None)
                            continue
                        text_value = str(observed.get("assistant_text") or "")
                        identity = str(observed.get("assistant_identity") or "")
                        fingerprint = hashlib.sha256(
                            f"{identity}\n{text_value}".encode("utf-8")
                        ).hexdigest()
                        previous = stable.get(request_id)
                        count = previous[1] + 1 if previous and previous[0] == fingerprint else 1
                        stable[request_id] = (fingerprint, count)
                        if count < RESULT_STABLE_OBSERVATIONS:
                            continue
                        try:
                            outcome = _record_completed_result(
                                layout, conversations, request, observed, now=now()
                            )
                        except (OSError, RuntimeError, ValueError) as exc:
                            _append_failure(failures, request_id, f"result finalization failed: {exc}")
                            completed.append(request_id)
                            continue
                        if outcome["child_state"] != "retired":
                            _append_failure(failures, request_id, "child did not reach retired state")
                            completed.append(request_id)
                            continue
                        failures.pop(request_id, None)
                        try:
                            _require_browser_result(
                                session.close_child(
                                    urls[request_id], ownerships[request_id]
                                ),
                                action="close_child",
                            )
                        except (OSError, RuntimeError, ValueError) as exc:
                            _append_failure(failures, request_id, f"retired cleanup failed: {exc}")
                        outcomes[request_id] = outcome
                        completed.append(request_id)
                    for request_id in completed:
                        pending.pop(request_id, None)
                    if pending and poll_seconds:
                        time.sleep(float(poll_seconds))
        finally:
            session.close()

    for request in requests:
        request_id = request["id"]
        if request_id in outcomes:
            continue
        message = failures.get(request_id, "child lifecycle is incomplete")
        outcomes[request_id] = _failure_outcome(conversations, request, message)

    workflow_state = workflow_store.load_state(str(spec["workflow_id"]))
    ordered = [outcomes[child["request_id"]] for child in spec["children"]]
    all_retired = all(item.get("child_state") == "retired" for item in ordered)
    status = (
        "completed"
        if not failures and all_retired and workflow_state["workflow_state"] == "completed"
        else "incomplete"
    )
    return {
        "schema_version": MVP_SCHEMA_VERSION,
        "status": status,
        "workflow_id": spec["workflow_id"],
        "workflow_state": workflow_state["workflow_state"],
        "failures": dict(sorted(failures.items())),
        "children": ordered,
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
    parser.add_argument("command", choices=("run",))
    parser.add_argument("--spec", required=True)
    parser.add_argument("--login-timeout-seconds", type=int, default=DEFAULT_LOGIN_TIMEOUT_SECONDS)
    parser.add_argument("--result-timeout-seconds", type=int, default=DEFAULT_RESULT_TIMEOUT_SECONDS)
    parser.add_argument("--home")
    parser.add_argument("--root")
    parser.add_argument("--checkout")
    parser.add_argument("--production-checkout")
    return parser.parse_args(list(argv) if argv is not None else None)


def main(argv: Iterable[str] | None = None) -> int:
    try:
        args = parse_args(argv)
        result = run_mvp_campaign(
            _layout_from_args(args),
            _load_spec(Path(args.spec)),
            login_timeout_seconds=args.login_timeout_seconds,
            result_timeout_seconds=args.result_timeout_seconds,
        )
        print(json.dumps(result, indent=2, ensure_ascii=False, sort_keys=True))
        return 0 if result.get("status") == "completed" else 2
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"DEV Conversation Fabric MVP error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
