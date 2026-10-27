"""Default-disabled synthetic result projection GitHub CAS writer.

The GitHub record is an explicitly UNATTESTED test projection, never a
consumer acknowledgement or real child completion. It may only follow
the already published exact public synthetic dispatch + semantic claims.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from local_agent.conversation import github_fabric_github as git
from local_agent.conversation import github_fabric_recovery as recovery
from local_agent.conversation import github_fabric_results as results

MAX_ATTEMPTS = 4


def _snapshot(api: Any, result_id: str) -> tuple[
    str, str, dict[str, Any] | None, dict[str, dict[str, Any] | None]
]:
    ref = api.request("GET", git.GITHUB_REF_PATH)
    head = git._require_sha(ref.get("object", {}).get("sha"), label="result origin head")
    commit = api.request("GET", f"/git/commits/{head}")
    tree = git._require_sha(commit.get("tree", {}).get("sha"), label="result origin tree")
    index = git._read_json_at_commit(
        api, results.INDEX_PATH, head, max_bytes=results.MAX_INDEX_BYTES
    )
    all_ids = set(results.validate_index(index)["result_ids"]) | {result_id}
    records = {
        identity: git._read_json_at_commit(
            api, results.result_path(identity), head, max_bytes=results.MAX_RESULT_BYTES
        )
        for identity in sorted(all_ids)
    }
    return head, tree, index, records


def _commit_atomic(api: Any, head: str, tree: str, plan: results.SyntheticResultPlan) -> None:
    if (plan.operation != "commit_atomic" or plan.expected_head_sha != head
            or len(plan.writes) != 2):
        raise ValueError("Synthetic result must commit record and index atomically")
    record_path, record = plan.writes[0]
    index_path, index = plan.writes[1]
    if (record_path != results.result_path(record["id"])
            or index_path != results.INDEX_PATH):
        raise ValueError("Synthetic result commit paths are not derived from identity")
    results.validate_projection(record)
    results.validate_index(index)
    entries = []
    for path, payload, maximum in (
        (record_path, record, results.MAX_RESULT_BYTES),
        (index_path, index, results.MAX_INDEX_BYTES),
    ):
        data = results._canonical(payload) + b"\n"
        if len(data) > maximum:
            raise ValueError("Synthetic result GitHub payload exceeds byte bound")
        blob = api.request("POST", "/git/blobs", {
            "content": data.decode("utf-8"), "encoding": "utf-8"
        })
        entries.append({
            "path": path, "mode": "100644", "type": "blob",
            "sha": git._require_sha(blob.get("sha"), label="synthetic result blob"),
        })
    tree_response = api.request(
        "POST", "/git/trees", {"base_tree": tree, "tree": entries}
    )
    new_tree_sha = git._require_sha(tree_response.get("sha"), label="result tree")
    new_commit = api.request("POST", "/git/commits", {
        "message": "Synthetic Fabric unattested result projection",
        "tree": new_tree_sha, "parents": [head],
    })
    new_head = git._require_sha(new_commit.get("sha"), label="result commit")
    api.request(
        "PATCH", git.GITHUB_REF_UPDATE_PATH, {"sha": new_head, "force": False}
    )


@dataclass(frozen=True, slots=True)
class SyntheticResultPublication:
    status: str
    head_sha: str
    result_id: str
    completed_commits: int


def publish_synthetic_result_fixture(
    operator_request: dict[str, Any],
    child_requests: list[dict[str, Any]],
    operator_result: dict[str, Any],
    *,
    enabled: bool = False,
    token: str | None = None,
    api: Any | None = None,
) -> SyntheticResultPublication:
    if enabled is not True:
        raise PermissionError("GitHub Fabric synthetic result publisher is disabled")
    # Reject any arbitrary/private source before making a network request.
    projection = results.build_projection(
        operator_request, child_requests, operator_result
    )
    if api is None:
        api = git.GitHubFabricREST(token)
    attempted = False
    committed = 0
    for _ in range(MAX_ATTEMPTS):
        observed = recovery.recover_public_synthetic_snapshot(
            operator_request, child_requests, api=api, enabled=True
        )
        head, tree, index, records = _snapshot(api, projection["id"])
        # The positive predecessor snapshot must be the same exact origin.
        if observed.source_head_sha != head:
            continue
        plan = results.preflight_synthetic_result(
            operator_request, child_requests, operator_result,
            existing_index=index, existing_records=records,
            expected_head_sha=head, enabled=True, writer_authorized=True,
        )
        if plan.operation == "replay":
            return SyntheticResultPublication(
                "converged" if attempted else "replay",
                head, projection["id"], committed
            )
        try:
            attempted = True
            _commit_atomic(api, head, tree, plan)
            committed += 1
        except git.GithubFabricHTTPError as exc:
            if exc.status not in {409, 422}:
                raise
        except git.GithubFabricTransportError:
            # A successful non-forced ref update may have lost its response.
            # Re-read the origin before ever considering another mutation.
            continue
    raise RuntimeError("GitHub Fabric synthetic result CAS did not converge")


@dataclass(frozen=True, slots=True)
class SyntheticResultObservation:
    """Unattested result-fixture existence at a specific source commit."""

    source_head_sha: str
    result_id: str
    kind: str
    ack_state: str
    execution_state: str
    operator_result_digest: str


def read_synthetic_result_projection(
    operator_request: dict[str, Any],
    child_requests: list[dict[str, Any]],
    operator_result: dict[str, Any],
    *,
    enabled: bool = False,
    token: str | None = None,
    api: Any | None = None,
) -> SyntheticResultObservation | None:
    """Observe verified immutable fixture records; NEVER infer child execution.

    None means the exact result projection is not yet durably indexed. An
    orphan, malformed, conflicting or incomplete record always raises.
    """
    if enabled is not True:
        raise PermissionError("GitHub Fabric synthetic result read is disabled")
    projection = results.build_projection(
        operator_request, child_requests, operator_result
    )
    if api is None:
        api = git.GitHubFabricREST(token)
    prior = recovery.recover_public_synthetic_snapshot(
        operator_request, child_requests, enabled=True, api=api
    )
    head, _tree, index, records = _snapshot(api, projection["id"])
    if head != prior.source_head_sha:
        raise ValueError("Synthetic result source advanced during recovery")
    plan = results.preflight_synthetic_result(
        operator_request, child_requests, operator_result,
        existing_index=index, existing_records=records,
        expected_head_sha=head, enabled=True, writer_authorized=True,
    )
    if plan.operation != "replay":
        return None
    return SyntheticResultObservation(
        source_head_sha=head,
        result_id=projection["id"],
        kind=results.SOURCE_KIND,
        ack_state=results.ACK_STATE,
        execution_state=results.EXECUTION_STATE,
        operator_result_digest=projection["operator_result_digest"],
    )
