"""Default-disabled private GitHub CAS for irreversible candidate Send intents.

Successful CAS records that a browser Send *might* have happened. This
module NEVER returns a capability or authorizes a browser effect. The
legacy and GitHub-first drivers are not yet joined to this protocol.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from local_agent.conversation import github_fabric_effect_intents as effects
from local_agent.conversation import github_fabric_github as git
from local_agent.conversation import github_fabric_parent_ownership_github as parents
from local_agent.conversation import github_fabric_private_github as private_git


class EffectIntentOutcomeUncertain(RuntimeError):
    """Remote CAS outcome cannot be proven; never reissue any Send."""


class EffectIntentConflict(RuntimeError):
    """A concurrent ref/owner update occurred; require manual reconciliation."""


@dataclass(frozen=True, slots=True)
class CandidateEffectResult:
    status: str
    source_head_sha: str
    parent_id: str
    child_request_id: str
    effect_id: str
    browser_send_authorized: bool = False


def _read_effect(api: Any, snapshot: parents.Snapshot) -> dict[str, Any] | None:
    if snapshot.parent is None:
        raise PermissionError("Private parent effect cannot exist without owner")
    path = effects.effect_path(snapshot.parent["id"])
    record = git._read_json_at_commit(
        api, path, snapshot.head, max_bytes=effects.MAX_RECORD_BYTES,
    )
    if record is not None:
        effects.validate_ledger(record, snapshot.parent, snapshot.dispatch)
    return record


def _commit_effect(
    api: Any, snapshot: parents.Snapshot, expected: dict[str, Any],
) -> None:
    if snapshot.parent is None:
        raise ValueError("Private effect commit requires parent ownership")
    effects.validate_ledger(expected, snapshot.parent, snapshot.dispatch)
    path = effects.effect_path(snapshot.parent["id"])
    data = json.dumps(
        expected, ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), allow_nan=False,
    ).encode("utf-8") + b"\n"
    if len(data) > effects.MAX_RECORD_BYTES:
        raise ValueError("Private parent effect ledger exceeds bound")
    blob = api.request("POST", "/git/blobs", {
        "content": data.decode("utf-8"), "encoding": "utf-8",
    })
    tree = api.request("POST", "/git/trees", {
        "base_tree": snapshot.tree,
        "tree": [{
            "path": path, "mode": "100644", "type": "blob",
            "sha": git._require_sha(blob.get("sha"), label="effect blob"),
        }],
    })
    commit = api.request("POST", "/git/commits", {
        "message": "Record candidate possibly-sent Fabric effect (no Send authority)",
        "tree": git._require_sha(tree.get("sha"), label="effect tree"),
        "parents": [snapshot.head],
    })
    api.request("PATCH", private_git.REF_UPDATE_PATH, {
        "sha": git._require_sha(commit.get("sha"), label="effect commit"),
        "force": False,
    })


def _reconcile(
    api: Any, dispatch_id: str, owner_id: str, epoch: int,
    expected: dict[str, Any],
) -> CandidateEffectResult:
    try:
        origin = parents._snapshot(api, dispatch_id)
        parent = origin.parent
        if (parent is None or parent["phase"] != "active"
                or parent["dispatch_id"] != dispatch_id
                or parent["owner_id"] != owner_id
                or parent["fence_epoch"] != epoch):
            raise EffectIntentConflict("Private effect CAS owner or epoch changed")
        current = _read_effect(api, origin)
    except EffectIntentConflict:
        raise
    except Exception as exc:
        raise EffectIntentOutcomeUncertain(
            "Private effect CAS outcome unreadable; no automatic replay"
        ) from exc
    if current != expected:
        raise EffectIntentConflict(
            "Private effect CAS record changed or absent; no automatic retry"
        )
    last = expected["effects"][-1]
    return CandidateEffectResult(
        "converged_no_send", origin.head, expected["parent_id"],
        last["child_request_id"], last["effect_id"],
    )


def _write_candidate(
    api: Any, origin: parents.Snapshot, expected: dict[str, Any],
) -> CandidateEffectResult:
    parent = origin.parent
    if parent is None:
        raise PermissionError("Private parent effect ownership missing")
    try:
        _commit_effect(api, origin, expected)
    except git.GithubFabricHTTPError as exc:
        if exc.status not in {409, 422}:
            raise
        return _reconcile(
            api, origin.dispatch["id"], parent["owner_id"],
            parent["fence_epoch"], expected,
        )
    except git.GithubFabricTransportError:
        # Lost ACK may follow a durable update. The only safe response is
        # read-only reconciliation, never an automatic repeat of Git PUT/Send.
        return _reconcile(
            api, origin.dispatch["id"], parent["owner_id"],
            parent["fence_epoch"], expected,
        )
    confirmed = _reconcile(
        api, origin.dispatch["id"], parent["owner_id"],
        parent["fence_epoch"], expected,
    )
    return CandidateEffectResult(
        "recorded_no_send", confirmed.source_head_sha,
        confirmed.parent_id, confirmed.child_request_id, confirmed.effect_id,
    )


def _origin(
    api: Any, dispatch_id: str, owner_id: str, epoch: int,
) -> tuple[parents.Snapshot, dict[str, Any] | None]:
    snapshot = parents._snapshot(api, dispatch_id)
    owner = snapshot.parent
    if (owner is None or owner["phase"] != "active"
            or owner["dispatch_id"] != dispatch_id
            or owner["owner_id"] != owner_id
            or type(epoch) is not int or owner["fence_epoch"] != epoch):
        raise PermissionError("Private effect active owner/epoch mismatch")
    return snapshot, _read_effect(api, snapshot)


def record_candidate_send_intent(
    dispatch_id: str, owner_id: str, epoch: int,
    child_request_id: str, *,
    enabled: bool = False, writer_authorized: bool = False,
    token: str | None = None, api: Any | None = None,
) -> CandidateEffectResult:
    """Atomically record possibly-sent intent. NEVER grant a Submit token."""
    if enabled is not True or writer_authorized is not True:
        raise PermissionError("Private parent effect writer default-disabled")
    if api is None:
        api = private_git.PrivateFabricREST(token)
    snapshot, prior = _origin(api, dispatch_id, owner_id, epoch)
    expected = effects.propose_send_intent(
        snapshot.parent, snapshot.dispatch, child_request_id, existing=prior
    )
    return _write_candidate(api, snapshot, expected)


def verify_candidate_child_result(
    dispatch_id: str, owner_id: str, epoch: int,
    child_request_id: str, *,
    enabled: bool = False, writer_authorized: bool = False,
    token: str | None = None, api: Any | None = None,
) -> CandidateEffectResult:
    """Only pinned independently completed receipts can advance the ledger."""
    if enabled is not True or writer_authorized is not True:
        raise PermissionError("Private parent effect verification default-disabled")
    if api is None:
        api = private_git.PrivateFabricREST(token)
    snapshot, prior = _origin(api, dispatch_id, owner_id, epoch)
    if prior is None:
        raise PermissionError("Private child result cannot precede a Send intent")
    summary = parents._receipt_summary(api, snapshot)
    expected = effects.propose_result_verification(
        snapshot.parent, snapshot.dispatch, child_request_id,
        summary, existing=prior,
    )
    return _write_candidate(api, snapshot, expected)


def read_candidate_effect_status(
    dispatch_id: str, *, enabled: bool = False,
    token: str | None = None, api: Any | None = None,
) -> dict[str, Any]:
    """One immutable private-GitHub snapshot, redacted observation only."""
    if enabled is not True:
        raise PermissionError("Private parent effect read default-disabled")
    if api is None:
        api = private_git.PrivateFabricREST(token)
    snapshot = parents._snapshot(api, dispatch_id)
    if snapshot.parent is None:
        raise PermissionError("Private parent effect owner absent")
    prior = _read_effect(api, snapshot)
    projection = effects.recovery_projection(
        snapshot.parent, snapshot.dispatch, prior,
    )
    return {
        "source_head_sha": snapshot.head,
        **projection,
    }
