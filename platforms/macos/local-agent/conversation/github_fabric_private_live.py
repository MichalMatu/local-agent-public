"""Trusted staging of real private Fabric dispatches, without browser authority.

A successful record/index commit is durable GitHub intake data, NOT an ACK,
a parent transport lease, or permission for Chat Bridge to press Send.
"""

from __future__ import annotations

import argparse
import json
import os
import re
from dataclasses import dataclass
from typing import Any

from local_agent.conversation import github_fabric_dispatch as dispatch_contract
from local_agent.conversation import github_fabric_github as git
from local_agent.conversation import github_fabric_private_github as private_git
from local_agent.conversation import github_fabric_private_publication as catalog

MAX_ATTEMPTS = 4
_REQUEST_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,119}\Z")
REQUEST_ROOT = "projects/local-agent/workflows/workflow-001/requests/"
REQUEST_INDEX = REQUEST_ROOT + "index.json"
PROJECT_ID = "local-agent"
WORKFLOW_ID = "workflow-001"


@dataclass(frozen=True, slots=True)
class PrivateDispatchPublication:
    status: str
    source_head_sha: str
    dispatch_id: str
    completed_commits: int


def _snapshot(api: Any, record_path: str) -> tuple[
    str, str, dict[str, Any] | None, dict[str, Any] | None,
    dict[str, Any] | None, dict[str, Any] | None
]:
    ref = api.request("GET", private_git.REF_PATH)
    if (ref.get("ref") != "refs/heads/" + private_git.PRIVATE_BRANCH
            or ref.get("object", {}).get("type") != "commit"):
        raise ValueError("Private Fabric live origin ref invalid")
    head = git._require_sha(ref.get("object", {}).get("sha"), label="private origin head")
    commit = api.request("GET", f"/git/commits/{head}")
    tree = git._require_sha(commit.get("tree", {}).get("sha"), label="private origin tree")
    values = [
        git._read_json_at_commit(api, path, head, max_bytes=limit)
        for path, limit in (
            (catalog.CATALOG_PATH, catalog.MAX_INDEX_BYTES),
            (catalog.WORKFLOWS_PATH, catalog.MAX_INDEX_BYTES),
            (catalog.INDEX_PATH, catalog.MAX_INDEX_BYTES),
            (record_path, catalog.MAX_RECORD_BYTES),
        )
    ]
    return head, tree, *values


def _validate_remote(
    projects: dict[str, Any] | None,
    workflows: dict[str, Any] | None,
    index: dict[str, Any] | None,
    record: dict[str, Any] | None,
    dispatch: dict[str, Any],
) -> tuple[str, dict[str, Any] | None]:
    if (projects is None or PROJECT_ID not in catalog.validate_projects(projects)
            or workflows is None
            or WORKFLOW_ID not in catalog.validate_workflows(workflows)):
        raise PermissionError("Private Fabric live project/workflow not indexed")
    ids = [] if index is None else catalog.validate_dispatch_index(index)
    identifier = dispatch["id"]
    if identifier in ids:
        if record is None:
            raise ValueError("Private Fabric indexed dispatch record missing")
        dispatch_contract.reconcile_github_fabric_dispatch(record, dispatch)
        return "replay", None
    if record is not None:
        raise ValueError("Private Fabric unindexed dispatch requires manual recovery")
    if len(ids) >= catalog.MAX_DISPATCH_IDS:
        raise ValueError("Private Fabric dispatch index at capacity")
    updated = {"schema_version": 1, "dispatch_ids": sorted([*ids, identifier])}
    catalog.validate_dispatch_index(updated)
    return "commit_atomic", updated


def _commit_atomic(
    api: Any, *, head: str, tree: str, dispatch: dict[str, Any],
    index: dict[str, Any],
) -> None:
    entries = []
    for path, payload, maximum in (
        (catalog.dispatch_path(dispatch["id"]), dispatch, catalog.MAX_RECORD_BYTES),
        (catalog.INDEX_PATH, index, catalog.MAX_INDEX_BYTES),
    ):
        data = dispatch_contract._canonical_bytes(payload) + b"\n"
        if len(data) > maximum:
            raise ValueError("Private Fabric live dispatch payload oversized")
        blob = api.request("POST", "/git/blobs", {
            "content": data.decode("utf-8"), "encoding": "utf-8"
        })
        entries.append({
            "path": path, "mode": "100644", "type": "blob",
            "sha": git._require_sha(blob.get("sha"), label="private live blob"),
        })
    new_tree = api.request("POST", "/git/trees", {
        "base_tree": tree, "tree": entries,
    })
    sha = git._require_sha(new_tree.get("sha"), label="private live tree")
    commit = api.request("POST", "/git/commits", {
        "message": "Stage private Fabric dispatch (no browser authority)",
        "tree": sha, "parents": [head],
    })
    new_head = git._require_sha(commit.get("sha"), label="private live commit")
    api.request("PATCH", private_git.REF_UPDATE_PATH, {
        "sha": new_head, "force": False,
    })


def stage_private_dispatch(
    operator_request: dict[str, Any],
    child_requests: list[dict[str, Any]],
    *,
    enabled: bool = False,
    writer_authorized: bool = False,
    token: str | None = None,
    api: Any | None = None,
) -> PrivateDispatchPublication:
    """Stage only an already validated, explicitly approved workflow in private GitHub.

    This function intentionally does not change a parent transport mode or
    schedule any browser effect. Failed/ambiguous GitHub writes are always
    followed by fresh immutable reads before another CAS attempt.
    """
    if enabled is not True or writer_authorized is not True:
        raise PermissionError("Private Fabric live staging is disabled")
    dispatch = dispatch_contract.build_github_fabric_dispatch(
        operator_request, child_requests
    )
    if operator_request["workflow_id"] != WORKFLOW_ID:
        raise PermissionError("Private Fabric live workflow is not approved")
    if api is None:
        api = private_git.PrivateFabricREST(token)
    record_path = catalog.dispatch_path(dispatch["id"])
    attempted = False
    commits = 0
    for _ in range(MAX_ATTEMPTS):
        head, tree, projects, workflows, index, record = _snapshot(api, record_path)
        operation, updated = _validate_remote(
            projects, workflows, index, record, dispatch
        )
        # Every indexed dispatch shares one immutable operator request ID.
        # A mutated private request cannot sneak through as a second dispatch.
        for other_id in [] if index is None else catalog.validate_dispatch_index(index):
            if other_id == dispatch["id"]:
                continue
            other = git._read_json_at_commit(
                api, catalog.dispatch_path(other_id), head,
                max_bytes=catalog.MAX_RECORD_BYTES,
            )
            if other is None:
                raise ValueError("Private Fabric indexed competing dispatch is missing")
            dispatch_contract.validate_github_fabric_dispatch(other)
            if other["id"] != other_id:
                raise ValueError("Private Fabric competing dispatch identity invalid")
            if other["request_id"] == dispatch["request_id"]:
                raise ValueError("Private Fabric same operator request ID has different dispatch")

        if operation == "replay":
            return PrivateDispatchPublication(
                "converged" if attempted else "replay", head, dispatch["id"], commits
            )
        if updated is None:
            raise AssertionError("Private Fabric live index update missing")
        try:
            attempted = True
            _commit_atomic(api, head=head, tree=tree, dispatch=dispatch, index=updated)
            commits += 1
        except git.GithubFabricHTTPError as exc:
            if exc.status not in {409, 422}:
                raise
        except git.GithubFabricTransportError:
            # A lost PATCH response does not imply failure. Re-read before retry.
            continue
    raise RuntimeError("Private Fabric live staging did not converge")


def stage_private_queued_request(
    request_id: str,
    *,
    enabled: bool = False,
    writer_authorized: bool = False,
    token: str | None = None,
    api: Any | None = None,
) -> PrivateDispatchPublication:
    """Resolve a private GitHub ChildRequest envelope through trusted Local Agent.

    Superchat writes only to the fixed private request namespace; a public
    agent-control task may carry ONLY the opaque request ID, never prompt text.
    """
    if enabled is not True or writer_authorized is not True:
        raise PermissionError("Private queued Fabric intake disabled")
    if not isinstance(request_id, str) or not _REQUEST_ID.fullmatch(request_id):
        raise ValueError("Private Fabric request ID invalid")
    if api is None:
        api = private_git.PrivateFabricREST(token)
    ref = api.request("GET", private_git.REF_PATH)
    if (ref.get("ref") != "refs/heads/" + private_git.PRIVATE_BRANCH
            or ref.get("object", {}).get("type") != "commit"):
        raise ValueError("Private Fabric queued request ref invalid")
    head = git._require_sha(ref.get("object", {}).get("sha"), label="private request head")
    index = git._read_json_at_commit(
        api, REQUEST_INDEX, head, max_bytes=catalog.MAX_INDEX_BYTES
    )
    if (not isinstance(index, dict) or set(index) != {"schema_version", "request_ids"}
            or type(index["schema_version"]) is not int or index["schema_version"] != 1
            or not isinstance(index["request_ids"], list)
            or len(index["request_ids"]) > 4
            or index["request_ids"] != sorted(set(index["request_ids"]))
            or any(not isinstance(item, str) or not _REQUEST_ID.fullmatch(item)
                   for item in index["request_ids"])
            or len(json.dumps(index).encode("utf-8")) > catalog.MAX_INDEX_BYTES):
        raise ValueError("Private Fabric request index invalid")
    if request_id not in index["request_ids"]:
        raise PermissionError("Private Fabric request is not indexed")
    source = git._read_json_at_commit(
        api, REQUEST_ROOT + request_id + ".json", head,
        max_bytes=catalog.MAX_RECORD_BYTES
    )
    if (not isinstance(source, dict)
            or set(source) != {"schema_version", "operator_request", "child_requests"}
            or type(source["schema_version"]) is not int
            or source["schema_version"] != 1
            or not isinstance(source["operator_request"], dict)
            or not isinstance(source["child_requests"], list)
            or source["operator_request"].get("id") != request_id):
        raise ValueError("Private Fabric queued request envelope invalid")
    return stage_private_dispatch(
        source["operator_request"], source["child_requests"],
        enabled=True, writer_authorized=True, api=api,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Stage an indexed private GitHub Fabric request")
    parser.add_argument("--request-id", required=True)
    parser.add_argument("--stage-private", action="store_true")
    args = parser.parse_args(argv)
    if not args.stage_private:
        parser.error("explicit --stage-private required")
    token = os.environ.get("LOCAL_AGENT_GITHUB_FABRIC_WRITE_TOKEN")
    if not token:
        parser.error("trusted Mac-side scoped GitHub token required")
    result = stage_private_queued_request(
        args.request_id, enabled=True, writer_authorized=True, token=token
    )
    print(json.dumps({
        "status": result.status, "dispatch_id": result.dispatch_id,
        "source_head_sha": result.source_head_sha,
        "completed_commits": result.completed_commits,
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
