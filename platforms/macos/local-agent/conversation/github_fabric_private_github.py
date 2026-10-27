"""Trusted private-repository GitHub writer for one known-public synthetic fixture.

No browser use, real private bootstrap publication, arbitrary project ID,
implicit token acquisition, or GitHub read/write credential in Chrome.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import (
    HTTPRedirectHandler, Request, build_opener,
)

from local_agent.conversation import github_fabric_github as git
from local_agent.conversation import github_fabric_private_publication as preflight

PRIVATE_REPOSITORY = "example-owner/local-agent-fabric-private"
PRIVATE_BRANCH = "fabric-data"
API_ROOT = "https://api.github.com/repos/" + PRIVATE_REPOSITORY
REF_PATH = "/git/ref/heads/" + PRIVATE_BRANCH
REF_UPDATE_PATH = "/git/refs/heads/" + PRIVATE_BRANCH
MAX_API_BYTES = 512 * 1024
MAX_ATTEMPTS = 8


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class PrivateFabricREST(git.GitHubFabricREST):
    """A non-redirecting token holder bound only to the private data repo."""

    def request(self, method: str, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        if (method not in {"GET", "POST", "PATCH"} or not path.startswith("/")
                or ".." in path or "//" in path or "\\" in path):
            raise ValueError("Private Fabric GitHub method/path invalid")
        if not (
            path in {REF_PATH, REF_UPDATE_PATH, "/git/blobs", "/git/trees", "/git/commits"}
            or path.startswith("/git/commits/")
            or path.startswith("/contents/projects/")
        ):
            raise ValueError("Private Fabric GitHub path outside allowed namespace")
        payload = None if body is None else json.dumps(
            body, ensure_ascii=False, sort_keys=True, allow_nan=False
        ).encode("utf-8")
        request = Request(
            API_ROOT + path, data=payload, method=method,
            headers={
                "Authorization": f"Bearer {self._token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "local-agent-private-fabric-synthetic",
                "Cache-Control": "no-store",
                **({"Content-Type": "application/json"} if payload is not None else {}),
            },
        )
        try:
            with build_opener(_NoRedirect()).open(request, timeout=15) as response:
                data = response.read(MAX_API_BYTES + 1)
        except HTTPError as exc:
            raise git.GithubFabricHTTPError(exc.code) from None
        except (URLError, TimeoutError, OSError):
            raise git.GithubFabricTransportError("Private Fabric GitHub transport failed") from None
        if len(data) > MAX_API_BYTES:
            raise ValueError("Private Fabric GitHub API response oversized")
        try:
            value = json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise ValueError("Private Fabric GitHub API JSON malformed") from None
        if not isinstance(value, dict):
            raise ValueError("Private Fabric GitHub API payload not an object")
        return value


def _origin(api: Any, record_path: str) -> tuple[
    str, str, dict[str, Any], dict[str, Any], dict[str, Any] | None,
    dict[str, Any] | None
]:
    remote = api.request("GET", REF_PATH)
    if (remote.get("ref") != "refs/heads/" + PRIVATE_BRANCH
            or remote.get("object", {}).get("type") != "commit"):
        raise ValueError("Private Fabric origin ref is invalid")
    head = git._require_sha(remote.get("object", {}).get("sha"), label="private branch head")
    commit = api.request("GET", f"/git/commits/{head}")
    tree = git._require_sha(commit.get("tree", {}).get("sha"), label="private origin tree")
    project_index = git._read_json_at_commit(
        api, preflight.CATALOG_PATH, head, max_bytes=preflight.MAX_INDEX_BYTES
    )
    workflows = git._read_json_at_commit(
        api, preflight.WORKFLOWS_PATH, head, max_bytes=preflight.MAX_INDEX_BYTES
    )
    index = git._read_json_at_commit(
        api, preflight.INDEX_PATH, head, max_bytes=preflight.MAX_INDEX_BYTES
    )
    record = git._read_json_at_commit(
        api, record_path, head, max_bytes=preflight.MAX_RECORD_BYTES
    )
    if project_index is None or workflows is None:
        raise ValueError("Private Fabric origin project or workflow index absent")
    return head, tree, project_index, workflows, index, record


def _commit(api: Any, head: str, tree: str, step: preflight.SyntheticPrivateStep) -> None:
    if step.operation not in {
        "create_record", "publish_dispatch_index", "publish_workflow_index"
    } or step.expected_head_sha != head or step.path is None or step.payload is None:
        raise ValueError("Private Fabric publishing plan is stale or unwritable")
    allowed = {
        preflight.WORKFLOWS_PATH,
        preflight.INDEX_PATH,
        preflight.dispatch_path(step.payload["id"])
        if step.operation == "create_record" else "",
    }
    if step.path not in allowed:
        raise ValueError("Private Fabric publishing path violates derived contract")
    data = preflight.public_guard._canonical_bytes(step.payload) + b"\n"
    if len(data) > preflight.MAX_RECORD_BYTES:
        raise ValueError("Private Fabric payload oversized")
    blob = api.request("POST", "/git/blobs", {
        "content": data.decode("utf-8"), "encoding": "utf-8"
    })
    blob_sha = git._require_sha(blob.get("sha"), label="private data blob")
    new_tree = api.request("POST", "/git/trees", {
        "base_tree": tree,
        "tree": [{"path": step.path, "mode": "100644", "type": "blob", "sha": blob_sha}],
    })
    tree_sha = git._require_sha(new_tree.get("sha"), label="private data tree")
    commit = api.request("POST", "/git/commits", {
        "message": "Synthetic private Fabric " + step.operation,
        "tree": tree_sha, "parents": [head],
    })
    update = git._require_sha(commit.get("sha"), label="private data commit")
    api.request("PATCH", REF_UPDATE_PATH, {"sha": update, "force": False})


@dataclass(frozen=True, slots=True)
class PrivateSyntheticPublication:
    status: str
    head_sha: str
    applied_steps: tuple[str, ...]


def publish_private_synthetic_fixture(
    operator_request: dict[str, Any],
    child_requests: list[dict[str, Any]],
    *,
    enabled: bool = False,
    token: str | None = None,
    api: Any | None = None,
) -> PrivateSyntheticPublication:
    if enabled is not True:
        raise PermissionError("Private Fabric publishing is default-disabled")
    # Hard-coded fixture approval happens before network access and includes
    # parent, bootstrap, child requests and allowed synthetic workflow.
    preflight.preflight_private_synthetic(
        operator_request, child_requests, enabled=True, writer_authorized=True,
        expected_head_sha="0" * 40,
        project_index={"schema_version": 1, "project_ids": [preflight.PROJECT_ID]},
        workflow_index={"schema_version": 1, "workflow_ids": []},
        dispatch_index=None, dispatch_record=None,
    )
    dispatch = preflight.dispatch_contract.build_github_fabric_dispatch(
        operator_request, child_requests
    )
    if api is None:
        api = PrivateFabricREST(token)
    record_path = preflight.dispatch_path(dispatch["id"])
    applied: list[str] = []
    for _ in range(MAX_ATTEMPTS):
        head, tree, project_index, workflows, index, record = _origin(api, record_path)
        plan = preflight.preflight_private_synthetic(
            operator_request, child_requests, enabled=True, writer_authorized=True,
            expected_head_sha=head, project_index=project_index,
            workflow_index=workflows, dispatch_index=index, dispatch_record=record,
        )
        if plan.operation == "replay":
            return PrivateSyntheticPublication(
                "converged" if applied else "replay", head, tuple(applied)
            )
        try:
            _commit(api, head, tree, plan)
            applied.append(plan.operation)
        except git.GithubFabricHTTPError as exc:
            if exc.status not in {409, 422}:
                raise
        except git.GithubFabricTransportError:
            # Reconcile the remote ref before deciding whether to mutate.
            continue
    raise RuntimeError("Private Fabric synthetic CAS did not converge")
