"""Read-only, commit-pinned reconstruction of the approved public Fabric fixture.

No browser state, local campaign, tab identity or synthetic claim is execution
authority. This preview verifies that the *same* Git origin contains the exact
approved dispatch, the immutable claim records and their discoverable indexes.
It is restricted to the known-public fixture, never arbitrary/private prompts.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from local_agent.conversation import github_fabric_claims as claims
from local_agent.conversation import github_fabric_dispatch as dispatch_model
from local_agent.conversation import github_fabric_github as git
from local_agent.conversation import github_fabric_publication as dispatch_publication

SCHEMA_VERSION = 1
SOURCE_KIND = "public_synthetic_observation_only"
UNCONFIRMED = "published_execution_unconfirmed"


@dataclass(frozen=True, slots=True)
class RecoveredSyntheticChild:
    claim_id: str
    workflow_node_id: str
    child_request_id: str
    spawn_transaction_id: str
    lifecycle: str


@dataclass(frozen=True, slots=True)
class RecoveredSyntheticSnapshot:
    schema_version: int
    source_kind: str
    source_head_sha: str
    parent_conversation_url: str
    workflow_id: str
    operator_request_id: str
    dispatch_id: str
    children: tuple[RecoveredSyntheticChild, ...]


def _load_at_head(api: Any, path: str, head: str, maximum: int) -> dict[str, Any] | None:
    return git._read_json_at_commit(api, path, head, max_bytes=maximum)


def recover_public_synthetic_snapshot(
    operator_request: dict[str, Any],
    child_requests: list[dict[str, Any]],
    *,
    enabled: bool = False,
    token: str | None = None,
    api: Any | None = None,
) -> RecoveredSyntheticSnapshot:
    """Rebuild read-only project state from exactly one trusted Git commit.

    The approved source fingerprint is verified *before* any remote I/O.
    No state is persisted locally and missing/conflicting remote evidence
    raises instead of implying that a child is eligible for another Send.
    """
    if enabled is not True:
        raise PermissionError("GitHub Fabric cold recovery is default-disabled")
    dispatch_publication.preflight_synthetic_publication(
        operator_request, child_requests,
        existing_record=None, existing_index=None,
        expected_head_sha="0" * 40,
        enabled=True, writer_authorized=True,
    )
    expected_dispatch = dispatch_model.build_github_fabric_dispatch(
        operator_request, child_requests
    )
    expected_claims = claims.build_claims(operator_request, child_requests)
    expected_by_id = {record["id"]: record for record in expected_claims}
    if api is None:
        api = git.GitHubFabricREST(token)

    branch = api.request("GET", git.GITHUB_REF_PATH)
    head = git._require_sha(branch.get("object", {}).get("sha"), label="recovery branch head")
    commit = api.request("GET", f"/git/commits/{head}")
    git._require_sha(commit.get("tree", {}).get("sha"), label="recovery source tree")

    dispatch_index_raw = _load_at_head(
        api, dispatch_publication.INDEX_PATH, head,
        dispatch_publication.MAX_INDEX_BYTES
    )
    if dispatch_index_raw is None:
        raise ValueError("GitHub Fabric dispatch index is missing")
    dispatch_index = dispatch_publication._validate_index(dispatch_index_raw)
    if expected_dispatch["id"] not in dispatch_index["dispatch_ids"]:
        raise ValueError("GitHub Fabric dispatch is not indexed")

    dispatch_path = dispatch_publication.RECORD_ROOT + expected_dispatch["id"] + ".json"
    stored_dispatch = _load_at_head(
        api, dispatch_path, head, dispatch_model.MAX_GITHUB_FABRIC_DISPATCH_BYTES
    )
    if stored_dispatch is None:
        raise ValueError("GitHub Fabric indexed dispatch record is missing")
    dispatch_model.reconcile_github_fabric_dispatch(stored_dispatch, expected_dispatch)

    claim_index_raw = _load_at_head(api, claims.INDEX_PATH, head, claims.MAX_INDEX_BYTES)
    if claim_index_raw is None:
        raise ValueError("GitHub Fabric claim index is missing")
    claim_index = claims.validate_index(claim_index_raw)
    if not set(expected_by_id).issubset(claim_index["claim_ids"]):
        raise ValueError("GitHub Fabric semantic claims are not fully indexed")

    # Verify all indexed records, not just the expected children. An unrelated
    # dangling index or malformed record makes this snapshot untrustworthy.
    records = {}
    for identifier in claim_index["claim_ids"]:
        record = _load_at_head(api, claims.claim_path(identifier), head, claims.MAX_CLAIM_BYTES)
        if record is None:
            raise ValueError("GitHub Fabric indexed semantic claim is missing")
        claims.validate_claim(record)
        if record["id"] != identifier:
            raise ValueError("GitHub Fabric claim path identity does not match record")
        records[identifier] = record
    for identifier, expected in expected_by_id.items():
        if claims._encoded(records[identifier]) != claims._encoded(expected):
            raise ValueError("GitHub Fabric semantic claim conflicts with approved source")

    nodes = tuple(
        RecoveredSyntheticChild(
            claim_id=record["id"],
            workflow_node_id=record["workflow_node_id"],
            child_request_id=record["child_request_id"],
            spawn_transaction_id=record["spawn_transaction_id"],
            lifecycle=UNCONFIRMED,
        )
        for record in expected_claims
    )
    return RecoveredSyntheticSnapshot(
        schema_version=SCHEMA_VERSION,
        source_kind=SOURCE_KIND,
        source_head_sha=head,
        parent_conversation_url=operator_request["parent_conversation_url"],
        workflow_id=operator_request["workflow_id"],
        operator_request_id=operator_request["id"],
        dispatch_id=expected_dispatch["id"],
        children=nodes,
    )
