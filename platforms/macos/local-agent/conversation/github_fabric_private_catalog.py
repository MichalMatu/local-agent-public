"""Default-disabled private Conversation Fabric project/workflow catalog observation.

Only bounded project and workflow identifiers are returned. No dispatch,
bootstrap text, browser ACK, result body, machine authority, or token is exposed.
All reads use a single pinned private GitHub commit SHA.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from local_agent.conversation import github_fabric_github as git
from local_agent.conversation import github_fabric_private_github as private_api
from local_agent.conversation import github_fabric_private_publication as schema

SOURCE_KIND = "private_project_catalog_observation_only"


@dataclass(frozen=True, slots=True)
class PrivateFabricProjectEntry:
    project_id: str
    workflow_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class PrivateFabricProjectCatalog:
    source_kind: str
    source_head_sha: str
    projects: tuple[PrivateFabricProjectEntry, ...]


def read_private_fabric_project_catalog(
    *,
    enabled: bool = False,
    token: str | None = None,
    api: Any | None = None,
) -> PrivateFabricProjectCatalog:
    """Return a bounded, source-pinned index-only project view.

    No project root or workflow record is ever inferred from an unindexed
    path. Missing metadata, invalid schema, inaccessible private data, or
    mutable origin errors fail closed; no attempt to repair files is made.
    """
    if enabled is not True:
        raise PermissionError("Private project catalog read is default-disabled")
    client = api if api is not None else private_api.PrivateFabricREST(token)
    ref = client.request("GET", private_api.REF_PATH)
    if (
        not isinstance(ref, dict)
        or ref.get("ref") != "refs/heads/" + private_api.PRIVATE_BRANCH
        or not isinstance(ref.get("object"), dict)
        or ref["object"].get("type") != "commit"
    ):
        raise ValueError("Private project catalog origin ref invalid")
    head = git._require_sha(
        ref["object"].get("sha"), label="private project catalog origin"
    )
    project_index = git._read_json_at_commit(
        client, schema.CATALOG_PATH, head, max_bytes=schema.MAX_INDEX_BYTES
    )
    if project_index is None:
        raise ValueError("Private project catalog is missing")
    project_ids = schema.validate_projects(project_index)
    entries: list[PrivateFabricProjectEntry] = []
    for project_id in project_ids:
        # Every name has already passed the strict bounded catalog validator;
        # callers never get to supply an arbitrary path or GitHub ref.
        path = f"projects/{project_id}/workflows/index.json"
        workflow_index = git._read_json_at_commit(
            client, path, head, max_bytes=schema.MAX_INDEX_BYTES
        )
        if workflow_index is None:
            raise ValueError("Private project workflow catalog is missing")
        workflow_ids = schema.validate_workflows(workflow_index)
        entries.append(PrivateFabricProjectEntry(
            project_id=project_id, workflow_ids=tuple(workflow_ids),
        ))
    return PrivateFabricProjectCatalog(
        source_kind=SOURCE_KIND,
        source_head_sha=head,
        projects=tuple(entries),
    )
