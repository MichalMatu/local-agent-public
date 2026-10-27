"""Private, commit-pinned, strictly read-only synthetic Fabric dispatch recovery.

This does NOT authorize any tab, composer, GitHub claim, child execution, ACK
or terminal result. Only the known-public test fixture is allowed until the
private source admission and parent-mode fencing protocols are implemented.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from local_agent.conversation import github_fabric_dispatch as dispatch_contract
from local_agent.conversation import github_fabric_github as git
from local_agent.conversation import github_fabric_private_github as private_git
from local_agent.conversation import github_fabric_private_publication as contract
from local_agent.conversation import github_fabric_publication as public_guard

SOURCE_KIND = "private_synthetic_dispatch_unattested"
EXECUTION_STATE = "published_execution_unconfirmed"


@dataclass(frozen=True, slots=True)
class PrivateSyntheticChildObservation:
    request_id: str
    spawn_transaction_id: str
    child_request_digest: str
    bootstrap_digest: str
    execution_state: str


@dataclass(frozen=True, slots=True)
class PrivateSyntheticDispatchObservation:
    source_kind: str
    source_head_sha: str
    project_id: str
    workflow_id: str
    parent_conversation_url: str
    operator_request_id: str
    dispatch_id: str
    children: tuple[PrivateSyntheticChildObservation, ...]


def recover_private_synthetic_dispatch(
    operator_request: dict[str, Any],
    child_requests: list[dict[str, Any]],
    *,
    enabled: bool = False,
    token: str | None = None,
    api: Any | None = None,
) -> PrivateSyntheticDispatchObservation:
    """Reconstruct a fully indexed synthetic dispatch from one private SHA.

    A missing or incomplete record fails closed. This reader never makes a
    Git mutation, retries a browser Send, or infers an actual consumer ACK.
    """
    if enabled is not True:
        raise PermissionError("Private GitHub Fabric cold recovery is disabled")
    public_guard.preflight_synthetic_publication(
        operator_request, child_requests,
        existing_record=None, existing_index=None,
        expected_head_sha="0" * 40,
        enabled=True, writer_authorized=True,
    )
    if operator_request["workflow_id"] != contract.WORKFLOW_ID:
        raise PermissionError("Private GitHub Fabric recovery workflow scope invalid")
    dispatch = dispatch_contract.build_github_fabric_dispatch(
        operator_request, child_requests
    )
    if api is None:
        api = private_git.PrivateFabricREST(token)
    head, _tree, projects, workflows, dispatch_index, stored = private_git._origin(
        api, contract.dispatch_path(dispatch["id"])
    )
    step = contract.preflight_private_synthetic(
        operator_request, child_requests,
        project_index=projects, workflow_index=workflows,
        dispatch_index=dispatch_index, dispatch_record=stored,
        expected_head_sha=head, enabled=True, writer_authorized=True,
    )
    if step.operation != "replay":
        raise ValueError("Private GitHub Fabric workflow is not fully indexed")
    if stored is None:
        raise ValueError("Private GitHub Fabric indexed dispatch is missing")
    dispatch_contract.reconcile_github_fabric_dispatch(stored, dispatch)
    children = tuple(
        PrivateSyntheticChildObservation(
            request_id=item["request_id"],
            spawn_transaction_id=item["spawn"]["transaction_id"],
            child_request_digest=item["spawn"]["child_request_digest"],
            bootstrap_digest=item["spawn"]["bootstrap_digest"],
            execution_state=EXECUTION_STATE,
        )
        for item in dispatch["children"]
    )
    return PrivateSyntheticDispatchObservation(
        source_kind=SOURCE_KIND,
        source_head_sha=git._require_sha(head, label="private snapshot"),
        project_id=contract.PROJECT_ID,
        workflow_id=contract.WORKFLOW_ID,
        parent_conversation_url=dispatch["parent_conversation_url"],
        operator_request_id=dispatch["request_id"],
        dispatch_id=dispatch["id"],
        children=children,
    )
