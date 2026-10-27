"""Default-disabled synthetic Git Data CAS for v2 parent/browser bindings.

The candidate binds identities only. It cannot authorize a browser Send,
perform a real Chrome effect, write a receipt or release an epoch.
Only an explicitly supplied Git Data API may be used by this source-only
prototype; there is no automatic credential or production activation.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from local_agent.conversation import github_fabric_browser_binding_v2 as binding
from local_agent.conversation import github_fabric_effect_intents as effects
from local_agent.conversation import github_fabric_github as git
from local_agent.conversation import github_fabric_parent_ownership_github as owners
from local_agent.conversation import github_fabric_private_github as private_git


class BrowserBindingWriteConflict(RuntimeError):
    """Remote binding or parent differs. Never retry the write or Send."""


class BrowserBindingWriteUncertain(RuntimeError):
    """Remote outcome cannot be authenticated. Never infer a Send permit."""


@dataclass(frozen=True, slots=True)
class CandidateBrowserBindingResult:
    status: str
    source_head_sha: str
    parent_id: str
    child_request_id: str
    binding_digest: str
    browser_send_authorized: bool = False


def _same_owner(
    snapshot: owners.Snapshot, dispatch_id: str, owner_id: str, epoch: int,
) -> bool:
    parent = snapshot.parent
    return bool(
        parent is not None
        and parent["phase"] == "active"
        and parent["dispatch_id"] == dispatch_id
        and parent["owner_id"] == owner_id
        and type(epoch) is int
        and parent["fence_epoch"] == epoch
    )


def _origin(
    api: Any, dispatch_id: str, owner_id: str, epoch: int,
    child_request_id: str, driver_session_id: str,
) -> tuple[owners.Snapshot, dict[str, Any]]:
    snapshot = owners._snapshot(api, dispatch_id)
    if not _same_owner(snapshot, dispatch_id, owner_id, epoch):
        raise PermissionError("Private browser binding parent owner/epoch not active")
    parent = snapshot.parent
    assert parent is not None
    candidate = binding.propose_binding(
        parent, snapshot.dispatch, child_request_id,
        driver_session_id=driver_session_id, source_head_sha=snapshot.head,
    )
    ledger = git._read_json_at_commit(
        api, effects.effect_path(parent["id"]), snapshot.head,
        max_bytes=effects.MAX_RECORD_BYTES,
    )
    if ledger is not None:
        effects.validate_ledger(ledger, parent, snapshot.dispatch)
        if any(item["child_request_id"] == child_request_id for item in ledger["effects"]):
            raise PermissionError("Private browser binding cannot follow child Send intent")
    for child in snapshot.dispatch["children"]:
        path = binding.binding_path(
            parent["id"], epoch, child["request_id"],
        )
        prior = git._read_json_at_commit(
            api, path, snapshot.head, max_bytes=binding.MAX_BINDING_BYTES,
        )
        if prior is not None:
            # An existing immutable file is never a reason to re-publish,
            # recover a one-shot permission, or trust an unpinned origin.
            if child["request_id"] == child_request_id:
                raise BrowserBindingWriteConflict("Private browser binding already exists")
            if prior.get("driver_session_id") == driver_session_id:
                raise BrowserBindingWriteConflict("Private browser driver already bound")
            binding.validate_binding(
                prior, parent, snapshot.dispatch,
                expected_origin_head_sha=prior.get("binding_origin_head_sha"),
            )
    return snapshot, candidate


def _commit(
    api: Any, snapshot: owners.Snapshot, candidate: dict[str, Any],
) -> None:
    parent = snapshot.parent
    if parent is None:
        raise PermissionError("Private browser binding parent missing")
    binding.validate_binding(
        candidate, parent, snapshot.dispatch,
        expected_origin_head_sha=snapshot.head,
    )
    path = binding.binding_path(
        parent["id"], parent["fence_epoch"], candidate["child_request_id"],
    )
    data = binding._canonical(candidate, binding.MAX_BINDING_BYTES) + b"\n"
    if len(data) > binding.MAX_BINDING_BYTES:
        raise ValueError("Private browser binding payload exceeds bound")
    blob = api.request("POST", "/git/blobs", {
        "content": data.decode("utf-8"), "encoding": "utf-8",
    })
    tree = api.request("POST", "/git/trees", {
        "base_tree": snapshot.tree,
        "tree": [{
            "path": path, "mode": "100644", "type": "blob",
            "sha": git._require_sha(blob.get("sha"), label="browser binding blob"),
        }],
    })
    commit = api.request("POST", "/git/commits", {
        "message": "Record synthetic v2 browser binding (no Send authority)",
        "tree": git._require_sha(tree.get("sha"), label="browser binding tree"),
        "parents": [snapshot.head],
    })
    api.request("PATCH", private_git.REF_UPDATE_PATH, {
        "sha": git._require_sha(commit.get("sha"), label="browser binding commit"),
        "force": False,
    })


def _confirm(
    api: Any, candidate: dict[str, Any],
) -> CandidateBrowserBindingResult:
    try:
        snapshot = owners._snapshot(api, candidate["dispatch_id"])
        if not _same_owner(
            snapshot, candidate["dispatch_id"], candidate["parent_owner_id"],
            candidate["fence_epoch"],
        ):
            raise BrowserBindingWriteConflict("Private browser parent owner changed")
        parent = snapshot.parent
        assert parent is not None
        path = binding.binding_path(
            parent["id"], parent["fence_epoch"], candidate["child_request_id"],
        )
        actual = git._read_json_at_commit(
            api, path, snapshot.head, max_bytes=binding.MAX_BINDING_BYTES,
        )
        if actual != candidate:
            raise BrowserBindingWriteConflict("Private browser CAS did not converge")
        digest = binding.binding_digest(
            actual, parent, snapshot.dispatch,
            expected_origin_head_sha=candidate["binding_origin_head_sha"],
        )
    except BrowserBindingWriteConflict:
        raise
    except Exception as exc:
        raise BrowserBindingWriteUncertain(
            "Private browser binding CAS outcome unreadable; no replay"
        ) from exc
    return CandidateBrowserBindingResult(
        "converged_no_send", snapshot.head, candidate["parent_id"],
        candidate["child_request_id"], digest,
    )


def publish_synthetic_browser_binding(
    dispatch_id: str, owner_id: str, epoch: int,
    child_request_id: str, *, driver_session_id: str,
    enabled: bool = False, writer_authorized: bool = False,
    api: Any | None = None,
) -> CandidateBrowserBindingResult:
    """One attempt only: no automatic ref retry and no browser capability."""
    if enabled is not True or writer_authorized is not True or api is None:
        raise PermissionError("Private synthetic browser binding writer default-disabled")
    origin, candidate = _origin(
        api, dispatch_id, owner_id, epoch, child_request_id, driver_session_id,
    )
    try:
        _commit(api, origin, candidate)
    except git.GithubFabricHTTPError as exc:
        if exc.status not in {409, 422}:
            raise
        return _confirm(api, candidate)
    except git.GithubFabricTransportError:
        return _confirm(api, candidate)
    confirmed = _confirm(api, candidate)
    return CandidateBrowserBindingResult(
        "recorded_no_send", confirmed.source_head_sha,
        confirmed.parent_id, confirmed.child_request_id, confirmed.binding_digest,
    )
