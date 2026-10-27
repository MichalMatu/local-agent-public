"""Trusted synthetic-only atomic claim/index GitHub commit adapter.

Unlike dispatch record-then-index publication, authority discovery must be
atomic: the semantic claim records and their index are committed as *one* Git
tree update. This module has no browser effects and never grants Send rights.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from local_agent.conversation import github_fabric_claims as claims
from local_agent.conversation import github_fabric_github as git

MAX_ATTEMPTS = 4


def _snapshot(api: Any, candidate_ids: tuple[str, ...]) -> tuple[
    str, str, dict[str, Any] | None, dict[str, dict[str, Any] | None]
]:
    ref = api.request("GET", git.GITHUB_REF_PATH)
    head = git._require_sha(ref.get("object", {}).get("sha"), label="claim origin head")
    commit = api.request("GET", f"/git/commits/{head}")
    tree = git._require_sha(commit.get("tree", {}).get("sha"), label="claim origin tree")
    index = git._read_json_at_commit(
        api, claims.INDEX_PATH, head, max_bytes=claims.MAX_INDEX_BYTES
    )
    ids = set(claims.validate_index(index)["claim_ids"]) | set(candidate_ids)
    records = {
        identifier: git._read_json_at_commit(
            api, claims.claim_path(identifier), head, max_bytes=claims.MAX_CLAIM_BYTES
        )
        for identifier in sorted(ids)
    }
    return head, tree, index, records


def _commit_atomic(api: Any, *, head: str, tree: str, plan: claims.SyntheticClaimPlan) -> None:
    if plan.operation != "commit_atomic" or not plan.writes:
        raise ValueError("Fabric claim update requires a non-empty atomic plan")
    if plan.expected_head_sha != head:
        raise ValueError("Fabric claim plan is stale")
    if len(plan.writes) > claims.MAX_CLAIMS + 1:
        raise ValueError("Fabric claim update exceeds atomic entry bound")
    if len({path for path, _ in plan.writes}) != len(plan.writes):
        raise ValueError("Fabric claim update repeats paths")
    entries = []
    for path, record in plan.writes:
        if path == claims.INDEX_PATH:
            claims.validate_index(record)
        elif path.startswith(claims.CLAIM_ROOT) and path.endswith(".json"):
            claims.validate_claim(record)
            if path != claims.claim_path(record["id"]):
                raise ValueError("Fabric claim update has inconsistent path")
        else:
            raise ValueError("Fabric claim update path is outside namespace")
        data = claims._encoded(record) + b"\n"
        if len(data) > claims.MAX_CLAIM_BYTES:
            raise ValueError("Fabric claim update payload exceeds size limit")
        blob = api.request(
            "POST", "/git/blobs", {"content": data.decode("utf-8"), "encoding": "utf-8"}
        )
        entries.append({
            "path": path, "mode": "100644", "type": "blob",
            "sha": git._require_sha(blob.get("sha"), label="claim blob"),
        })
    new_tree = api.request("POST", "/git/trees", {"base_tree": tree, "tree": entries})
    tree_sha = git._require_sha(new_tree.get("sha"), label="atomic claim tree")
    new_commit = api.request("POST", "/git/commits", {
        "message": "Synthetic Fabric semantic claim admission preview",
        "tree": tree_sha, "parents": [head],
    })
    commit_sha = git._require_sha(new_commit.get("sha"), label="atomic claim commit")
    api.request("PATCH", git.GITHUB_REF_UPDATE_PATH, {"sha": commit_sha, "force": False})


@dataclass(frozen=True, slots=True)
class SyntheticClaimsResult:
    status: str
    head_sha: str
    claim_ids: tuple[str, ...]
    completed_commits: int


def publish_synthetic_claims(
    operator_request: dict[str, Any],
    child_requests: list[dict[str, Any]],
    *,
    enabled: bool = False,
    token: str | None = None,
    api: Any | None = None,
) -> SyntheticClaimsResult:
    if enabled is not True:
        raise PermissionError("Synthetic semantic claim publishing is disabled")
    # Enforce the known-public fixture *before any network request*, including
    # read-only GETs. No caller-supplied 'synthetic' boolean is authorization.
    claims.preflight_synthetic_claims(
        operator_request, child_requests,
        existing_index=None,
        existing_records={
            record["id"]: None
            for record in claims.build_claims(operator_request, child_requests)
        },
        expected_head_sha="0" * 40, enabled=True, writer_authorized=True,
    )
    if api is None:
        api = git.GitHubFabricREST(token)
    records = claims.build_claims(operator_request, child_requests)
    ids = tuple(record["id"] for record in records)
    successful_commits = 0
    attempted = False
    for _ in range(MAX_ATTEMPTS):
        head, tree, index, existing_records = _snapshot(api, ids)
        plan = claims.preflight_synthetic_claims(
            operator_request, child_requests,
            existing_index=index, existing_records=existing_records,
            expected_head_sha=head, enabled=True, writer_authorized=True,
        )
        if plan.operation == "replay":
            return SyntheticClaimsResult(
                "converged" if attempted else "replay", head, ids, successful_commits
            )
        try:
            attempted = True
            _commit_atomic(api, head=head, tree=tree, plan=plan)
            successful_commits += 1
        except git.GithubFabricHTTPError as exc:
            if exc.status not in {409, 422}:
                raise
        except git.GithubFabricTransportError:
            # Lost ACK may follow a successful ref update: never blindly retry.
            continue
    raise RuntimeError("Fabric semantic claim atomic CAS did not converge")
