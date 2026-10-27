"""Project-scoped, default-disabled private GitHub Fabric *synthetic* writer plan.

This planner admits exactly the known-public test fixture only. It cannot
authorize real private text, child execution, a browser Send or a machine task.
Project/workflow IDs are derived from trusted provenance, never a page URL.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterable

from local_agent.conversation import github_fabric_dispatch as dispatch_contract
from local_agent.conversation import github_fabric_publication as public_guard

PROJECT_ID = "local-agent"
WORKFLOW_ID = "workflow-001"
SCHEMA_VERSION = 1
MAX_PROJECT_IDS = 32
MAX_WORKFLOW_IDS = 128
MAX_DISPATCH_IDS = 128
MAX_INDEX_BYTES = 8192
MAX_RECORD_BYTES = 128 * 1024
CATALOG_PATH = "projects/index.json"
WORKFLOWS_PATH = f"projects/{PROJECT_ID}/workflows/index.json"
DISPATCH_ROOT = f"projects/{PROJECT_ID}/workflows/{WORKFLOW_ID}/dispatches/"
INDEX_PATH = DISPATCH_ROOT + "index.json"
_HEAD = re.compile(r"[0-9a-f]{40}\Z")
_PROJECT = re.compile(r"[a-z0-9][a-z0-9-]{0,63}\Z")
_WORKFLOW = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,119}\Z")
_DISPATCH = re.compile(r"fabric-[0-9a-f]{32}\Z")


def _catalog(value: Any, *, key: str, limit: int,
             pattern: re.Pattern[str]) -> list[str]:
    if (not isinstance(value, dict)
            or set(value) != {"schema_version", key}
            or type(value["schema_version"]) is not int
            or value["schema_version"] != SCHEMA_VERSION
            or not isinstance(value[key], list) or len(value[key]) > limit
            or any(not isinstance(i, str) or not pattern.fullmatch(i) for i in value[key])
            or value[key] != sorted(set(value[key]))
            or len(public_guard._canonical_bytes(value)) > MAX_INDEX_BYTES):
        raise ValueError(f"Private Fabric {key} catalog invalid")
    return list(value[key])


def validate_projects(value: Any) -> list[str]:
    return _catalog(value, key="project_ids", limit=MAX_PROJECT_IDS, pattern=_PROJECT)


def validate_workflows(value: Any) -> list[str]:
    return _catalog(value, key="workflow_ids", limit=MAX_WORKFLOW_IDS, pattern=_WORKFLOW)


def validate_dispatch_index(value: Any) -> list[str]:
    return _catalog(value, key="dispatch_ids", limit=MAX_DISPATCH_IDS, pattern=_DISPATCH)


def dispatch_path(dispatch_id: str) -> str:
    if not isinstance(dispatch_id, str) or not _DISPATCH.fullmatch(dispatch_id):
        raise ValueError("Private Fabric dispatch path invalid")
    return DISPATCH_ROOT + dispatch_id + ".json"


@dataclass(frozen=True, slots=True)
class SyntheticPrivateStep:
    operation: str
    path: str | None
    payload: dict[str, Any] | None
    expected_head_sha: str


def preflight_private_synthetic(
    operator_request: dict[str, Any],
    child_requests: Iterable[dict[str, Any]],
    *,
    project_index: dict[str, Any],
    workflow_index: dict[str, Any],
    dispatch_index: dict[str, Any] | None,
    dispatch_record: dict[str, Any] | None,
    expected_head_sha: str,
    enabled: bool = False,
    writer_authorized: bool = False,
) -> SyntheticPrivateStep:
    if enabled is not True or writer_authorized is not True:
        raise PermissionError("Private Fabric publishing is default-disabled")
    if not isinstance(expected_head_sha, str) or not _HEAD.fullmatch(expected_head_sha):
        raise ValueError("Private Fabric publication requires exact origin SHA")
    requests = list(child_requests)
    public_guard.preflight_synthetic_publication(
        operator_request, requests, existing_record=None, existing_index=None,
        expected_head_sha=expected_head_sha, enabled=True, writer_authorized=True,
    )
    if operator_request["workflow_id"] != WORKFLOW_ID:
        raise PermissionError("Private Fabric synthetic project scope invalid")

    if PROJECT_ID not in validate_projects(project_index):
        raise ValueError("Private Fabric project has no authorized catalog entry")
    workflow_ids = validate_workflows(workflow_index)
    dispatch_ids = (
        [] if dispatch_index is None else validate_dispatch_index(dispatch_index)
    )
    dispatch = dispatch_contract.build_github_fabric_dispatch(
        operator_request, requests
    )
    record_path = dispatch_path(dispatch["id"])
    if dispatch_record is None:
        if dispatch["id"] in dispatch_ids:
            raise ValueError("Private Fabric indexed dispatch record missing")
        if WORKFLOW_ID in workflow_ids:
            raise ValueError("Private Fabric workflow index references incomplete dispatch")
        return SyntheticPrivateStep("create_record", record_path, dispatch, expected_head_sha)

    dispatch_contract.reconcile_github_fabric_dispatch(dispatch_record, dispatch)
    if dispatch["id"] not in dispatch_ids:
        if WORKFLOW_ID in workflow_ids:
            raise ValueError("Private Fabric published workflow references unindexed dispatch")
        if len(dispatch_ids) >= MAX_DISPATCH_IDS:
            raise ValueError("Private Fabric dispatch index at capacity")
        payload = {"schema_version": 1, "dispatch_ids": sorted([*dispatch_ids, dispatch["id"]])}
        validate_dispatch_index(payload)
        return SyntheticPrivateStep("publish_dispatch_index", INDEX_PATH, payload, expected_head_sha)

    if WORKFLOW_ID not in workflow_ids:
        if len(workflow_ids) >= MAX_WORKFLOW_IDS:
            raise ValueError("Private Fabric workflow catalog at capacity")
        payload = {"schema_version": 1, "workflow_ids": sorted([*workflow_ids, WORKFLOW_ID])}
        validate_workflows(payload)
        return SyntheticPrivateStep("publish_workflow_index", WORKFLOWS_PATH, payload, expected_head_sha)

    return SyntheticPrivateStep("replay", None, None, expected_head_sha)
