"""Create the single non-executing Stage 8 reasoning child from DEV checkout truth.

This is intentionally narrower than a general campaign authoring API. The first real
Conversation Fabric slice is bound to the local-agent development checkout, derives
repository identity from Git and the checked-in binding catalog, and admits exactly one
reasoning ChildRequest. It never creates a browser tab or an execution task.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlparse

from local_agent.conversation import contract as conversation_contract
from local_agent.conversation.store import WorkflowConversationStore
from local_agent.development.lab import (
    DevLabLayout,
    build_dev_lab_layout,
    dev_lab_status,
    validate_dev_lab_layout,
)
from local_agent.repository.binding import AgentBindingRecord, load_binding_catalog
from local_agent.workflow import contract as workflow_contract
from local_agent.workflow import publishing
from local_agent.workflow.store import WorkflowStore

LIVE_SEED_WORKFLOW_ID = "stage8-live-slice"
LIVE_SEED_NODE_ID = "stage8-reasoning-child"
LIVE_SEED_REQUEST_ID = "stage8-live-child-001"
LIVE_SEED_REPOSITORY_ID = "local-agent"
LIVE_SEED_REPOSITORY = "example-owner/local-agent"
LIVE_SEED_SCOPE_SUMMARY = (
    "Verify the bounded Stage 8 Conversation Fabric live slice. Do not request or queue "
    "Local Agent execution. Stay inside the admitted bootstrap and return a concise "
    "verification assessment for the parent campaign."
)
LIVE_SEED_SCOPE_PATHS = [
    "docs/conversation_fabric",
    "local_agent/conversation",
    "local_agent/development",
]
_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_DEV_BRANCH_RE = re.compile(r"^(?:develop/conversation-fabric|work/conversation-[A-Za-z0-9._/-]+)$")


@dataclass(frozen=True, slots=True)
class CheckoutIdentity:
    repository_id: str
    repository: str
    agent_binding: str
    repository_ref: str
    repository_commit_sha: str


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _git(checkout: Path, *args: str) -> str:
    command = ["git", "-C", str(checkout), *args]
    try:
        completed = subprocess.run(
            command,
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=15,
        )
    except FileNotFoundError as exc:
        raise RuntimeError("git is required for the DEV live-slice seed") from exc
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"DEV checkout git command timed out: {' '.join(args)}") from exc
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.stdout or "git command failed").strip()
        raise RuntimeError(
            f"DEV checkout git command failed ({' '.join(args)}): {detail}"
        ) from exc
    return completed.stdout.strip()


def _canonical_github_repository(remote: str) -> str:
    value = remote.strip()
    if value.startswith("git@github.com:"):
        path = value.removeprefix("git@github.com:")
    else:
        parsed = urlparse(value)
        if parsed.scheme not in {"https", "ssh"} or parsed.hostname != "github.com":
            raise RuntimeError("DEV checkout origin must point to github.com")
        path = parsed.path.lstrip("/")
    if path.endswith(".git"):
        path = path[:-4]
    if path.casefold() != LIVE_SEED_REPOSITORY.casefold():
        raise RuntimeError(
            "DEV checkout origin repository mismatch: "
            f"expected {LIVE_SEED_REPOSITORY!r}, got {path!r}"
        )
    return LIVE_SEED_REPOSITORY


def _binding_record(checkout: Path) -> AgentBindingRecord:
    catalog = checkout / "config" / "agent_bindings.json"
    if catalog.is_symlink() or not catalog.is_file():
        raise RuntimeError(f"DEV binding catalog is unavailable or unsafe: {catalog}")
    matches = [
        record
        for record in load_binding_catalog(catalog)
        if record.repository_id.casefold() == LIVE_SEED_REPOSITORY_ID.casefold()
        and record.repository.casefold() == LIVE_SEED_REPOSITORY.casefold()
    ]
    if len(matches) != 1:
        raise RuntimeError("DEV binding catalog must contain exactly one local-agent identity")
    record = matches[0]
    if record.execution_enabled:
        raise RuntimeError(
            "Stage 8 live seed requires local-agent execution_enabled=false"
        )
    return record


def inspect_live_seed_checkout(layout: DevLabLayout) -> CheckoutIdentity:
    """Return exact immutable source identity for a clean isolated DEV checkout."""
    validate_dev_lab_layout(layout)
    status = dev_lab_status(layout)
    if not status["healthy"]:
        problems = ",".join(status["problems"])
        raise RuntimeError(f"DEV lab must be initialized and healthy before seed: {problems}")

    checkout = layout.checkout.expanduser().resolve(strict=False)
    if checkout.is_symlink() or not checkout.is_dir():
        raise RuntimeError(f"DEV checkout is unavailable or unsafe: {checkout}")
    top = Path(_git(checkout, "rev-parse", "--show-toplevel")).resolve(strict=False)
    if top != checkout:
        raise RuntimeError(
            f"DEV checkout must be the Git worktree root: expected {checkout}, got {top}"
        )

    repository = _canonical_github_repository(_git(checkout, "remote", "get-url", "origin"))
    branch = _git(checkout, "symbolic-ref", "--quiet", "--short", "HEAD")
    if not _DEV_BRANCH_RE.fullmatch(branch):
        raise RuntimeError(
            "Stage 8 live seed requires develop/conversation-fabric or a work/conversation-* branch; "
            f"got {branch!r}"
        )
    sha = _git(checkout, "rev-parse", "HEAD")
    if not _SHA_RE.fullmatch(sha):
        raise RuntimeError("DEV checkout HEAD must be a canonical 40-character commit SHA")
    dirty = _git(checkout, "status", "--porcelain=v1", "--untracked-files=all")
    if dirty:
        raise RuntimeError("DEV checkout must be clean before creating the live-slice seed")

    record = _binding_record(checkout)
    return CheckoutIdentity(
        repository_id=record.repository_id,
        repository=repository,
        agent_binding=record.agent_binding,
        repository_ref=branch,
        repository_commit_sha=sha,
    )


def _canonical_parent_url(value: str) -> str:
    canonical = conversation_contract.canonical_conversation_url(value)
    if canonical != value:
        raise ValueError(
            "parent conversation URL must already use canonical https://chatgpt.com/c/<id> form"
        )
    return canonical


def _manifest(identity: CheckoutIdentity, *, created_at: str) -> dict[str, Any]:
    manifest = {
        "schema_version": workflow_contract.WORKFLOW_SCHEMA_VERSION,
        "id": LIVE_SEED_WORKFLOW_ID,
        "created_at": created_at,
        "nodes": [
            {
                "id": LIVE_SEED_NODE_ID,
                "kind": "reasoning",
                "repository_id": identity.repository_id,
                "agent_binding": identity.agent_binding,
                "depends_on": [],
            }
        ],
    }
    workflow_contract.validate_workflow_manifest(manifest)
    return manifest


def _request(
    manifest: dict[str, Any],
    identity: CheckoutIdentity,
    *,
    parent_conversation_url: str,
    created_at: str,
) -> dict[str, Any]:
    revision, introduction_digest = publishing.node_introduction_identity(
        manifest,
        [],
        LIVE_SEED_NODE_ID,
    )
    request = {
        "schema_version": conversation_contract.CHILD_REQUEST_SCHEMA_VERSION,
        "id": LIVE_SEED_REQUEST_ID,
        "workflow_id": LIVE_SEED_WORKFLOW_ID,
        "workflow_node_id": LIVE_SEED_NODE_ID,
        "workflow_node_revision": revision,
        "workflow_node_introduction_digest": introduction_digest,
        "parent_conversation_url": parent_conversation_url,
        "created_at": created_at,
        "role": "verification",
        "repository_id": identity.repository_id,
        "agent_binding": identity.agent_binding,
        "repository_ref": identity.repository_ref,
        "repository_commit_sha": identity.repository_commit_sha,
        "scope": {
            "summary": LIVE_SEED_SCOPE_SUMMARY,
            "paths": list(LIVE_SEED_SCOPE_PATHS),
        },
        "context_refs": [],
        "bootstrap_contract_version": conversation_contract.BOOTSTRAP_CONTRACT_VERSION,
    }
    conversation_contract.validate_child_request(request)
    return request


def _same_seed_request(
    durable: dict[str, Any],
    expected: dict[str, Any],
) -> bool:
    candidate = dict(expected)
    candidate["created_at"] = durable.get("created_at")
    try:
        conversation_contract.validate_child_request(candidate)
    except ValueError:
        return False
    return conversation_contract.child_request_digest(durable) == conversation_contract.child_request_digest(
        candidate
    )


def seed_live_slice(
    layout: DevLabLayout,
    *,
    parent_conversation_url: str,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Persist or verify the one exact non-executing Stage 8 workflow/request."""
    parent_url = _canonical_parent_url(parent_conversation_url)
    identity = inspect_live_seed_checkout(layout)
    current = (now or _utc_now()).astimezone(timezone.utc)
    workflow_store = WorkflowStore(layout.state_dir)

    workflow_ids = workflow_store.workflow_ids()
    if LIVE_SEED_WORKFLOW_ID in workflow_ids:
        manifest = workflow_store.load_manifest(LIVE_SEED_WORKFLOW_ID)
        expected_manifest = _manifest(identity, created_at=str(manifest.get("created_at", "")))
        if workflow_contract.manifest_digest(manifest) != workflow_contract.manifest_digest(
            expected_manifest
        ):
            raise RuntimeError("existing Stage 8 live workflow conflicts with current DEV identity")
        created_workflow = False
    else:
        manifest = _manifest(identity, created_at=_iso(current))
        workflow_store.submit(manifest)
        created_workflow = True

    conversations = WorkflowConversationStore(workflow_store, LIVE_SEED_WORKFLOW_ID)
    existing = conversations.request_for_node(LIVE_SEED_NODE_ID)
    if existing is not None:
        expected_request = _request(
            manifest,
            identity,
            parent_conversation_url=parent_url,
            created_at=str(existing.get("created_at", "")),
        )
        if existing.get("id") != LIVE_SEED_REQUEST_ID or not _same_seed_request(
            existing,
            expected_request,
        ):
            raise RuntimeError(
                "existing Stage 8 child request conflicts with parent URL or current DEV checkout"
            )
        request = existing
        created_request = False
    else:
        request = _request(
            manifest,
            identity,
            parent_conversation_url=parent_url,
            created_at=_iso(current),
        )
        conversations.admit_request(request)
        request = conversations.load_request(LIVE_SEED_REQUEST_ID)
        created_request = True

    workflow_state = workflow_store.load_state(LIVE_SEED_WORKFLOW_ID)
    child_state = conversations.load_state(LIVE_SEED_REQUEST_ID)
    if workflow_state["node_states"].get(LIVE_SEED_NODE_ID) != "waiting_conversation":
        raise RuntimeError("Stage 8 reasoning node is no longer waiting_conversation")
    if child_state["state"] != "requested":
        raise RuntimeError("Stage 8 child request is no longer in requested state")

    return {
        "workflow_id": LIVE_SEED_WORKFLOW_ID,
        "workflow_node_id": LIVE_SEED_NODE_ID,
        "request_id": LIVE_SEED_REQUEST_ID,
        "request_digest": conversation_contract.child_request_digest(request),
        "parent_conversation_url": parent_url,
        "repository_id": identity.repository_id,
        "repository": identity.repository,
        "agent_binding": identity.agent_binding,
        "repository_ref": identity.repository_ref,
        "repository_commit_sha": identity.repository_commit_sha,
        "execution_enabled": False,
        "workflow_state": workflow_state["workflow_state"],
        "child_state": child_state["state"],
        "created_workflow": created_workflow,
        "created_request": created_request,
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
    parser.add_argument("--parent-conversation-url", required=True)
    parser.add_argument("--home")
    parser.add_argument("--root")
    parser.add_argument("--checkout")
    parser.add_argument("--production-checkout")
    return parser.parse_args(list(argv) if argv is not None else None)


def main(argv: Iterable[str] | None = None) -> int:
    try:
        args = parse_args(argv)
        payload = seed_live_slice(
            _layout_from_args(args),
            parent_conversation_url=str(args.parent_conversation_url),
        )
        print(__import__("json").dumps(payload, indent=2, ensure_ascii=False, sort_keys=True))
        return 0
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"DEV live seed error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
