"""Pinned, read-only private GitHub evidence projection for a Fabric dispatch.

No prompt or answer text leaves this module's returned aggregate. A private
GitHub commit is the snapshot boundary; a missing record is never proof that
the corresponding browser side effect did not occur.
"""

from __future__ import annotations

import argparse
import json
import os
from typing import Any

from local_agent.conversation import (
    github_fabric_github as git,
    github_fabric_private_github as private_git,
    github_fabric_private_publication as catalog,
)
from local_agent.conversation.github_fabric_dispatch import validate_github_fabric_dispatch
from local_agent.conversation.github_fabric_receipt_aggregation import (
    aggregate_private_child_receipts,
)

RECEIPT_ROOT = "projects/local-agent/workflows/workflow-001/receipts/"
MAX_RECEIPT_BYTES = 24 * 1024


def read_private_dispatch_summary(
    dispatch_id: str,
    *,
    enabled: bool = False,
    token: str | None = None,
    api: Any | None = None,
) -> dict[str, Any]:
    """Read every child's claim/ACK/result at ONE validated private branch head."""
    if enabled is not True:
        raise PermissionError("Private Fabric summary reader is default-disabled")
    dispatch_path = catalog.dispatch_path(dispatch_id)
    if api is None:
        api = private_git.PrivateFabricREST(token)
    remote = api.request("GET", private_git.REF_PATH)
    if (remote.get("ref") != "refs/heads/" + private_git.PRIVATE_BRANCH
            or remote.get("object", {}).get("type") != "commit"):
        raise ValueError("Private Fabric summary origin ref invalid")
    head = git._require_sha(remote.get("object", {}).get("sha"), label="summary head")
    project_index = git._read_json_at_commit(
        api, catalog.CATALOG_PATH, head, max_bytes=catalog.MAX_INDEX_BYTES
    )
    workflow_index = git._read_json_at_commit(
        api, catalog.WORKFLOWS_PATH, head, max_bytes=catalog.MAX_INDEX_BYTES
    )
    dispatch_index = git._read_json_at_commit(
        api, catalog.INDEX_PATH, head, max_bytes=catalog.MAX_INDEX_BYTES
    )
    if (project_index is None
            or catalog.PROJECT_ID not in catalog.validate_projects(project_index)
            or workflow_index is None
            or catalog.WORKFLOW_ID not in catalog.validate_workflows(workflow_index)
            or dispatch_index is None
            or dispatch_id not in catalog.validate_dispatch_index(dispatch_index)):
        raise PermissionError("Private Fabric summary dispatch is not indexed")

    dispatch = git._read_json_at_commit(
        api, dispatch_path, head, max_bytes=catalog.MAX_RECORD_BYTES
    )
    if dispatch is None:
        raise ValueError("Private Fabric summary indexed dispatch missing")
    validate_github_fabric_dispatch(dispatch)
    if dispatch["id"] != dispatch_id:
        raise ValueError("Private Fabric summary indexed identity mismatch")

    receipts: dict[str, dict[str, dict[str, Any]]] = {}
    for child in dispatch["children"]:
        child_id = child["request_id"]
        evidence: dict[str, dict[str, Any]] = {}
        for kind in ("claim", "ack", "result"):
            path = f"{RECEIPT_ROOT}{dispatch_id}/{kind}/{child_id}.json"
            record = git._read_json_at_commit(
                api, path, head, max_bytes=MAX_RECEIPT_BYTES
            )
            if record is not None:
                evidence[kind] = record
        receipts[child_id] = evidence

    result = aggregate_private_child_receipts(dispatch, receipts)
    return {"source_head_sha": head, **result}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Read pinned private Fabric child evidence")
    parser.add_argument("--dispatch-id", required=True)
    parser.add_argument("--read-private", action="store_true")
    args = parser.parse_args(argv)
    if not args.read_private:
        parser.error("explicit --read-private required")
    token = os.environ.get("LOCAL_AGENT_GITHUB_FABRIC_WRITE_TOKEN")
    if not token:
        parser.error("trusted Mac-side scoped private GitHub credential required")
    result = read_private_dispatch_summary(
        args.dispatch_id, enabled=True, token=token
    )
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
