"""Exact-branch GitHub writer for the public synthetic Fabric fixture only.

This trusted Local Agent-side adapter uses GitHub Git Data APIs and requires an
explicit credential and opt-in. It never runs in Chrome and does not create a
child chat, execute a machine task, or enable any runtime feature flag.

Publication consists of two independent fast-forward-only commits: immutable
record first, then index. Every step is planned from a fresh GitHub ref and
commit-pinned file snapshot. A failed or lost acknowledgement is resolved by
reading GitHub again, never by blindly repeating a write.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
from pathlib import Path
from dataclasses import dataclass
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import HTTPRedirectHandler, Request, build_opener

from local_agent.conversation import github_fabric_publication as preflight

GITHUB_API_ROOT = "https://api.github.com/repos/example-owner/local-agent"
GITHUB_BRANCH = "chat-bridge-state"
GITHUB_REF_PATH = f"/git/ref/heads/{GITHUB_BRANCH}"
GITHUB_REF_UPDATE_PATH = f"/git/refs/heads/{GITHUB_BRANCH}"
MAX_API_RESPONSE_BYTES = 512 * 1024
MAX_ATTEMPTS = 4
_SHA_RE = re.compile(r"[0-9a-f]{40}\Z")


class GithubFabricHTTPError(RuntimeError):
    def __init__(self, status: int) -> None:
        super().__init__(f"GitHub Fabric API HTTP {status}")
        self.status = status


class GithubFabricTransportError(RuntimeError):
    pass


class _NoRedirect(HTTPRedirectHandler):
    """Never forward a Bearer token to a redirected URL."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class GitHubFabricREST:
    """Explicit credential holder on the trusted Local Agent side only."""

    def __init__(self, token: str) -> None:
        if (
            not isinstance(token, str)
            or not 1 <= len(token) <= 4096
            or token != token.strip()
            or any(ord(char) < 33 or ord(char) > 126 for char in token)
        ):
            raise PermissionError("GitHub Fabric publisher requires an explicit API token")
        self._token = token

    def request(
        self, method: str, path: str, body: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        if method not in {"GET", "POST", "PATCH"} or not path.startswith("/"):
            raise ValueError("GitHub Fabric API request is invalid")
        if "//" in path or ".." in path:
            raise ValueError("GitHub Fabric API path is invalid")
        if method == "GET" and body is not None:
            raise ValueError("GitHub Fabric GET cannot carry a body")
        if method == "PATCH":
            if (path != GITHUB_REF_UPDATE_PATH or not isinstance(body, dict)
                    or set(body) != {"sha", "force"} or body["force"] is not False):
                raise ValueError("GitHub Fabric ref update requires force=false")
            _require_sha(body["sha"], label="ref update")
        if method == "POST" and not isinstance(body, dict):
            raise ValueError("GitHub Fabric POST requires an object body")
        data = None if body is None else json.dumps(
            body, ensure_ascii=False, sort_keys=True, allow_nan=False
        ).encode("utf-8")
        request = Request(
            GITHUB_API_ROOT + path,
            data=data,
            method=method,
            headers={
                "Authorization": f"Bearer {self._token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "local-agent-github-fabric-synthetic",
                "Cache-Control": "no-store",
                **({"Content-Type": "application/json"} if data is not None else {}),
            },
        )
        try:
            with build_opener(_NoRedirect()).open(request, timeout=15) as response:
                data_bytes = response.read(MAX_API_RESPONSE_BYTES + 1)
        except HTTPError as exc:
            raise GithubFabricHTTPError(exc.code) from None
        except (URLError, TimeoutError, OSError) as exc:
            raise GithubFabricTransportError("GitHub Fabric API transport failed") from exc
        if len(data_bytes) > MAX_API_RESPONSE_BYTES:
            raise ValueError("GitHub Fabric API response exceeds bound")
        try:
            result = json.loads(data_bytes.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("GitHub Fabric API returned invalid JSON") from exc
        if not isinstance(result, dict):
            raise ValueError("GitHub Fabric API response must be an object")
        return result


def _require_sha(value: Any, *, label: str) -> str:
    if not isinstance(value, str) or not _SHA_RE.fullmatch(value):
        raise ValueError(f"GitHub Fabric {label} SHA is invalid")
    return value


def _read_json_at_commit(
    api: Any, path: str, head_sha: str, *, max_bytes: int
) -> dict[str, Any] | None:
    relative = quote(path, safe="/")
    try:
        response = api.request("GET", f"/contents/{relative}?ref={head_sha}")
    except GithubFabricHTTPError as exc:
        if exc.status == 404:
            return None
        raise
    if (
        response.get("type") != "file"
        or response.get("encoding") != "base64"
        or not isinstance(response.get("content"), str)
        or type(response.get("size")) is not int
        or not 0 <= response["size"] <= max_bytes
    ):
        raise ValueError("GitHub Fabric remote file has unsafe content metadata")
    try:
        raw = base64.b64decode(response["content"].replace("\n", ""), validate=True)
    except (ValueError, base64.binascii.Error) as exc:
        raise ValueError("GitHub Fabric remote file has invalid base64") from exc
    if len(raw) != response["size"] or len(raw) > max_bytes:
        raise ValueError("GitHub Fabric remote file violates size bound")
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("GitHub Fabric remote file has invalid JSON") from exc
    if not isinstance(value, dict):
        raise ValueError("GitHub Fabric remote JSON must be an object")
    return value


def _snapshot(api: Any, record_path: str) -> tuple[str, str, dict[str, Any] | None, dict[str, Any] | None]:
    ref = api.request("GET", GITHUB_REF_PATH)
    head = _require_sha(ref.get("object", {}).get("sha"), label="branch head")
    commit = api.request("GET", f"/git/commits/{head}")
    tree_sha = _require_sha(commit.get("tree", {}).get("sha"), label="commit tree")
    record = _read_json_at_commit(
        api, record_path, head, max_bytes=preflight.MAX_PREFLIGHT_INPUT_BYTES
    )
    index = _read_json_at_commit(
        api, preflight.INDEX_PATH, head, max_bytes=preflight.MAX_INDEX_BYTES
    )
    return head, tree_sha, record, index


def _commit_step(api: Any, *, head: str, tree: str, step: preflight.SyntheticPublicationStep) -> None:
    if step.operation not in {"create_record", "update_index"} or step.path is None or step.payload is None:
        raise ValueError("GitHub Fabric publication step is not writable")
    if step.expected_head_sha != head:
        raise ValueError("GitHub Fabric publication step has a stale head")
    data = json.dumps(
        step.payload, ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), allow_nan=False,
    ) + "\n"
    if len(data.encode("utf-8")) > preflight.MAX_PREFLIGHT_INPUT_BYTES:
        raise ValueError("GitHub Fabric published payload exceeds bound")
    blob = api.request("POST", "/git/blobs", {"content": data, "encoding": "utf-8"})
    blob_sha = _require_sha(blob.get("sha"), label="blob")
    new_tree = api.request(
        "POST", "/git/trees",
        {
            "base_tree": tree,
            "tree": [{"path": step.path, "mode": "100644", "type": "blob", "sha": blob_sha}],
        },
    )
    new_tree_sha = _require_sha(new_tree.get("sha"), label="new tree")
    commit = api.request(
        "POST", "/git/commits",
        {
            "message": f"Synthetic GitHub Fabric {step.operation}",
            "tree": new_tree_sha,
            "parents": [head],
        },
    )
    new_head = _require_sha(commit.get("sha"), label="new commit")
    api.request("PATCH", GITHUB_REF_UPDATE_PATH, {"sha": new_head, "force": False})


@dataclass(frozen=True, slots=True)
class SyntheticPublicationResult:
    dispatch_id: str
    status: str
    head_sha: str
    applied_steps: tuple[str, ...]


def publish_synthetic_fixture(
    operator_request: dict[str, Any],
    child_requests: list[dict[str, Any]],
    *,
    enabled: bool = False,
    token: str | None = None,
    api: Any | None = None,
) -> SyntheticPublicationResult:
    """Publish only the exact public fixture with commit-pinned CAS semantics.

    API injection exists solely for deterministic tests. A production call
    needs an explicit token and enabled=True; no implicit environment token.
    Permission failures and malformed remote state always fail closed.
    """
    if enabled is not True:
        raise PermissionError("GitHub Fabric synthetic publisher is disabled")
    if api is None:
        api = GitHubFabricREST(token)
    # The existing deterministic builder derives the path. The preflight also
    # pins the entire source fixture before approving a write.
    from local_agent.conversation.github_fabric_dispatch import build_github_fabric_dispatch

    dispatch_id = build_github_fabric_dispatch(operator_request, child_requests)["id"]
    record_path = preflight.RECORD_ROOT + dispatch_id + ".json"
    steps: list[str] = []
    mutation_attempted = False
    for _attempt in range(MAX_ATTEMPTS):
        head, tree, existing_record, existing_index = _snapshot(api, record_path)
        step = preflight.preflight_synthetic_publication(
            operator_request, child_requests,
            existing_record=existing_record,
            existing_index=existing_index,
            expected_head_sha=head,
            enabled=True,
            writer_authorized=True,
        )
        if step.operation == "replay":
            return SyntheticPublicationResult(
                dispatch_id, "published" if mutation_attempted else "replay", head, tuple(steps)
            )
        try:
            mutation_attempted = True
            _commit_step(api, head=head, tree=tree, step=step)
            steps.append(step.operation)
        except GithubFabricHTTPError as exc:
            if exc.status not in {409, 422}:
                raise
            # A non-fast-forward or update race is retried only with a fresh
            # pinned remote snapshot and a newly derived operation.
        except GithubFabricTransportError:
            # A timeout may have happened *after* the server changed the ref.
            # Never retry the same mutation without a new origin read.
            continue
    raise RuntimeError("GitHub Fabric publication did not converge after bounded rechecks")

def main(argv: list[str] | None = None) -> int:
    """Explicit Mac-side entrypoint for an allowlisted, public synthetic fixture."""
    parser = argparse.ArgumentParser(description="Publish the exact synthetic Fabric fixture")
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--publish-public-synthetic", action="store_true")
    args = parser.parse_args(argv)

    if not args.publish_public_synthetic:
        parser.error("explicit --publish-public-synthetic is required")
    token = os.environ.get("LOCAL_AGENT_GITHUB_FABRIC_WRITE_TOKEN")
    if not token:
        parser.error("LOCAL_AGENT_GITHUB_FABRIC_WRITE_TOKEN is required")
    path = args.source
    if path.is_symlink() or not path.is_file():
        parser.error("synthetic source must be a regular file")
    if path.stat().st_size > preflight.MAX_PREFLIGHT_INPUT_BYTES:
        parser.error("synthetic source exceeds size bound")
    try:
        source = json.loads(path.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, OSError) as exc:
        raise ValueError("synthetic source is invalid JSON") from exc
    if (
        not isinstance(source, dict)
        or set(source) != {"operator_request", "child_requests"}
        or not isinstance(source["operator_request"], dict)
        or not isinstance(source["child_requests"], list)
    ):
        raise ValueError("synthetic source fields are invalid")
    result = publish_synthetic_fixture(
        source["operator_request"],
        source["child_requests"],
        enabled=True,
        token=token,
    )
    print(json.dumps({
        "dispatch_id": result.dispatch_id,
        "status": result.status,
        "head_sha": result.head_sha,
        "applied_steps": list(result.applied_steps),
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
