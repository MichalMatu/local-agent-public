"""Explicit, authenticated GET-only live smoke for private synthetic Fabric.

The gh CLI supplies its existing authenticated session; no credential is read,
passed, logged, stored or embedded in the command line by this module.
This is not browser execution authority, a child ACK, or private-data admission.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
from dataclasses import dataclass
from typing import Any, Callable

from local_agent.conversation import github_fabric_private_catalog as project_catalog
from local_agent.conversation import github_fabric_private_github as private_api
from local_agent.conversation import github_fabric_private_publication as paths
from local_agent.conversation import github_fabric_private_recovery as recovery

MAX_GH_RESPONSE_BYTES = 512 * 1024
_SHA = re.compile(r"[0-9a-f]{40}\Z")
_DISPATCH = re.compile(r"fabric-[0-9a-f]{32}\Z")
_PARENT_RECORD = re.compile(r"parents/parent-[0-9a-f]{32}\.json\Z")
_PARENT_TREE = re.compile(r"/git/trees/[0-9a-f]{40}\?recursive=1\Z")
MAX_TREE_ENTRIES = 4096
_PROJECT_WORKFLOWS = re.compile(r"projects/[a-z0-9][a-z0-9-]{0,63}/workflows/index\.json\Z")
_PRIVATE_REPO_API = "repos/" + private_api.PRIVATE_REPOSITORY
_GIT_REF = private_api.REF_PATH
_SCOPED_CONTENT = frozenset({
    paths.CATALOG_PATH, paths.WORKFLOWS_PATH, paths.INDEX_PATH,
})


def _is_permitted_get(path: Any) -> bool:
    if not isinstance(path, str):
        return False
    if path == _GIT_REF:
        return True
    if _PARENT_TREE.fullmatch(path):
        return True
    if path.startswith("/git/commits/"):
        return bool(_SHA.fullmatch(path[len("/git/commits/"):]))
    if not path.startswith("/contents/"):
        return False
    name, delimiter, ref = path[len("/contents/"):].partition("?ref=")
    if delimiter != "?ref=" or not _SHA.fullmatch(ref):
        return False
    if name in _SCOPED_CONTENT or _PROJECT_WORKFLOWS.fullmatch(name):
        return True
    if not name.startswith(paths.DISPATCH_ROOT) or not name.endswith(".json"):
        return False
    identifier = name[len(paths.DISPATCH_ROOT):-len(".json")]
    return bool(_DISPATCH.fullmatch(identifier))


class GithubCliReadOnlyAdapter:
    """Strictly GET-only GitHub API adapter backed by the authenticated gh CLI."""

    def __init__(self, *, run: Callable[..., Any] | None = None) -> None:
        self._run = run if run is not None else subprocess.run

    def request(self, method: str, path: str, body: Any = None) -> dict[str, Any]:
        if method != "GET" or body is not None or not _is_permitted_get(path):
            raise PermissionError("Private synthetic smoke refuses unsafe GitHub API request")
        argv = ["gh", "api", "--method", "GET", _PRIVATE_REPO_API + path]
        try:
            completed = self._run(
                argv, capture_output=True, timeout=25, check=False,
            )
        except (subprocess.TimeoutExpired, OSError):
            raise RuntimeError("Private synthetic read transport unavailable") from None
        # Deliberately discard stderr: error messages from a local CLI are not
        # trusted to be safe for logs and can contain credential-bearing URLs.
        if completed.returncode != 0:
            raise RuntimeError("Private synthetic GitHub read failed")
        stdout = completed.stdout
        if not isinstance(stdout, bytes) or len(stdout) > MAX_GH_RESPONSE_BYTES:
            raise ValueError("Private synthetic GitHub response violates byte bound")
        try:
            result = json.loads(stdout.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise ValueError("Private synthetic GitHub read returned malformed JSON") from None
        if not isinstance(result, dict):
            raise ValueError("Private synthetic GitHub response must be an object")
        return result


@dataclass(frozen=True, slots=True)
class ReadOnlyPrivateSyntheticSmoke:
    source_head_sha: str
    dispatch_id: str
    child_count: int
    reads: int
    execution_state: str


def verify_private_synthetic_live_read(
    *, enabled: bool = False, api: Any = None,
) -> ReadOnlyPrivateSyntheticSmoke:
    if enabled is not True:
        raise PermissionError("Private synthetic live read smoke is disabled")
    # Import the fixed fixture only after the explicit opt-in.
    from tests.test_github_fabric_dispatch import admitted_children, operator_request

    source = api if api is not None else GithubCliReadOnlyAdapter()
    first = recovery.recover_private_synthetic_dispatch(
        operator_request(), admitted_children(), enabled=True, api=source,
    )
    # A separate, stateless second reconstruction checks that the source
    # stays stable without relying on a browser tab or process-local cache.
    second = recovery.recover_private_synthetic_dispatch(
        operator_request(), admitted_children(), enabled=True, api=source,
    )
    if first != second:
        raise ValueError("Private synthetic recovery changed across two pinned reads")
    if (not first.children or len(first.children) != 2
            or any(child.execution_state != recovery.EXECUTION_STATE
                   for child in first.children)):
        raise ValueError("Private synthetic recovery fabricated execution evidence")
    return ReadOnlyPrivateSyntheticSmoke(
        source_head_sha=first.source_head_sha,
        dispatch_id=first.dispatch_id,
        child_count=len(first.children),
        reads=2,
        execution_state=recovery.EXECUTION_STATE,
    )


def verify_private_project_catalog_live_read(
    *, enabled: bool = False, api: Any = None,
) -> project_catalog.PrivateFabricProjectCatalog:
    if enabled is not True:
        raise PermissionError("Private project catalog smoke is disabled")
    source = api if api is not None else GithubCliReadOnlyAdapter()
    first = project_catalog.read_private_fabric_project_catalog(
        enabled=True, api=source,
    )
    second = project_catalog.read_private_fabric_project_catalog(
        enabled=True, api=source,
    )
    if first != second:
        raise ValueError("Private project catalog changed across pinned reads")
    return first


@dataclass(frozen=True, slots=True)
class ReadOnlyPrivateParentNamespaceSmoke:
    source_head_sha: str
    status: str
    reads: int
    browser_effects_permitted: bool


def _read_private_parent_namespace_once(api: Any) -> ReadOnlyPrivateParentNamespaceSmoke:
    ref = api.request("GET", private_api.REF_PATH)
    if (ref.get("ref") != "refs/heads/" + private_api.PRIVATE_BRANCH
            or ref.get("object", {}).get("type") != "commit"):
        raise ValueError("Private parent origin ref invalid")
    head = ref["object"]["sha"]
    if not isinstance(head, str) or not _SHA.fullmatch(head):
        raise ValueError("Private parent origin SHA invalid")
    commit = api.request("GET", f"/git/commits/{head}")
    tree_sha = commit.get("tree", {}).get("sha")
    if not isinstance(tree_sha, str) or not _SHA.fullmatch(tree_sha):
        raise ValueError("Private parent origin tree invalid")
    listing = api.request("GET", f"/git/trees/{tree_sha}?recursive=1")
    entries = listing.get("tree")
    if (listing.get("sha") != tree_sha
            or listing.get("truncated") is not False
            or not isinstance(entries, list)
            or len(entries) > MAX_TREE_ENTRIES):
        raise ValueError("Private parent tree observation incomplete")
    for item in entries:
        if not isinstance(item, dict) or not isinstance(item.get("path"), str):
            raise ValueError("Private parent tree entry invalid")
        name = item["path"]
        if name == "parents":
            if item.get("type") != "tree":
                raise ValueError("Private parent namespace root corrupt")
        elif name.startswith("parents/"):
            if (name != "parents/index.json" and not _PARENT_RECORD.fullmatch(name)):
                raise ValueError("Private parent namespace contains unexpected path")
            raise ValueError("Private parent namespace requires manual review")
    return ReadOnlyPrivateParentNamespaceSmoke(
        source_head_sha=head, status="no_parent_records_observed", reads=2,
        browser_effects_permitted=False,
    )


def verify_private_parent_namespace_empty(
    *, enabled: bool = False, api: Any = None,
) -> ReadOnlyPrivateParentNamespaceSmoke:
    """Read actual private branch twice; never infer Send or browser ACK."""
    if enabled is not True:
        raise PermissionError("Private parent namespace observation is disabled")
    source = api if api is not None else GithubCliReadOnlyAdapter()
    first = _read_private_parent_namespace_once(source)
    second = _read_private_parent_namespace_once(source)
    if first != second:
        raise ValueError("Private parent origin changed across pinned reads")
    return first


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--verify-private-synthetic-read", action="store_true")
    mode.add_argument("--verify-private-catalog-read", action="store_true")
    mode.add_argument("--verify-private-parent-namespace-empty", action="store_true")
    args = parser.parse_args(argv)
    if args.verify_private_parent_namespace_empty:
        observed = verify_private_parent_namespace_empty(enabled=True)
        print(json.dumps({
            "source_head_sha": observed.source_head_sha,
            "status": observed.status, "reads": observed.reads,
            "browser_effects_permitted": observed.browser_effects_permitted,
            "ack_state": "not_attested",
        }, sort_keys=True))
    elif args.verify_private_catalog_read:
        observed = verify_private_project_catalog_live_read(enabled=True)
        print(json.dumps({
            "source_head_sha": observed.source_head_sha,
            "source_kind": observed.source_kind,
            "projects": {
                project.project_id: list(project.workflow_ids)
                for project in observed.projects
            },
            "reads": 2,
        }, sort_keys=True))
    else:
        found = verify_private_synthetic_live_read(enabled=True)
        print(json.dumps({
            "source_head_sha": found.source_head_sha,
            "dispatch_id": found.dispatch_id,
            "child_count": found.child_count,
            "reads": found.reads,
            "execution_state": found.execution_state,
            "ack_state": "not_attested",
        }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
