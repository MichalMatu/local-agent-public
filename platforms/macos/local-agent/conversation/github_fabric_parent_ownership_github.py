"""Default-disabled private GitHub CAS candidate for global Fabric parent ownership.

This writes ONLY candidate parent evidence, never browser Send authorization.
The installed/legacy Chat Bridge does not consult this namespace. Therefore
successful CAS is NOT a global exclusion proof or an execution grant.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from local_agent.conversation import github_fabric_github as git
from local_agent.conversation import github_fabric_effect_epoch_archive as effect_archives
from local_agent.conversation import github_fabric_effect_intents as effects
from local_agent.conversation import github_fabric_parent_ownership as policy
from local_agent.conversation import github_fabric_private_github as private_git
from local_agent.conversation import github_fabric_private_publication as catalog
from local_agent.conversation import github_fabric_dispatch as dispatch_contract
from local_agent.conversation.github_fabric_receipt_aggregation import (
    aggregate_owner_bound_private_child_receipts,
)
from local_agent.conversation.github_fabric_private_summary import (
    MAX_RECEIPT_BYTES, RECEIPT_ROOT,
)

INDEX_PATH = policy.ROOT + "index.json"
HISTORY_ROOT = policy.ROOT + "history/"
EPOCH_ROOT = policy.ROOT + "epochs/"
MAX_PARENTS = 16
MAX_EPOCHS = 32
MAX_INDEX_BYTES = 4096
MAX_HISTORY_BYTES = 8192
MAX_RECORD_BYTES = 4096
_DISPATCH = re.compile(r"fabric-[0-9a-f]{32}\Z")
_PARENT = re.compile(r"parent-[0-9a-f]{32}\Z")
_OWNER = re.compile(r"[0-9a-f]{32}\Z")


class ParentCandidateWriteUncertain(RuntimeError):
    """GitHub may have accepted an update; never automatically repeat it."""


class ParentCandidateConflict(RuntimeError):
    """CAS rejected or the remote identity diverged; do not seize ownership."""


@dataclass(frozen=True, slots=True)
class ParentCandidateResult:
    status: str
    source_head_sha: str
    parent_id: str
    fence_epoch: int
    browser_send_authorized: bool = False


@dataclass(frozen=True, slots=True)
class Snapshot:
    head: str
    tree: str
    dispatch: dict[str, Any]
    index: dict[str, Any]
    parent: dict[str, Any] | None
    history: dict[str, Any] | None


def history_path(identifier: str) -> str:
    policy.path(identifier)
    return HISTORY_ROOT + identifier + ".json"


def epoch_path(identifier: str, epoch: int) -> str:
    policy.path(identifier)
    if type(epoch) is not int or not 1 <= epoch <= MAX_EPOCHS:
        raise ValueError("Private parent epoch path invalid")
    return EPOCH_ROOT + identifier + "/" + f"{epoch:04d}.json"


def validate_index(value: dict[str, Any] | None) -> dict[str, Any]:
    if value is None:
        return {"schema_version": 1, "parent_ids": []}
    if (not isinstance(value, dict)
            or set(value) != {"schema_version", "parent_ids"}
            or type(value["schema_version"]) is not int or value["schema_version"] != 1
            or not isinstance(value["parent_ids"], list)
            or len(value["parent_ids"]) > MAX_PARENTS
            or any(not isinstance(item, str) or not _PARENT.fullmatch(item)
                   for item in value["parent_ids"])
            or value["parent_ids"] != sorted(set(value["parent_ids"]))
            or len(git.preflight._canonical_bytes(value)) > MAX_INDEX_BYTES):
        raise ValueError("Private parent index invalid")
    return value


def validate_history(value: Any, identifier: str) -> dict[str, Any]:
    policy.path(identifier)
    if (not isinstance(value, dict)
            or set(value) != {"schema_version", "parent_id", "entries"}
            or type(value["schema_version"]) is not int or value["schema_version"] != 1
            or value["parent_id"] != identifier
            or not isinstance(value["entries"], list)
            or not 1 <= len(value["entries"]) <= MAX_EPOCHS
            or len(git.preflight._canonical_bytes(value)) > MAX_HISTORY_BYTES):
        raise ValueError("Private parent epoch history invalid")
    dispatches: set[str] = set()
    owners: set[str] = set()
    for idx, item in enumerate(value["entries"], 1):
        if (not isinstance(item, dict)
                or set(item) != {"epoch", "dispatch_id", "owner_id"}
                or type(item["epoch"]) is not int or item["epoch"] != idx
                or not isinstance(item["dispatch_id"], str)
                or not _DISPATCH.fullmatch(item["dispatch_id"])
                or not isinstance(item["owner_id"], str)
                or not _OWNER.fullmatch(item["owner_id"])
                or item["dispatch_id"] in dispatches or item["owner_id"] in owners):
            raise ValueError("Private parent epoch history identity conflict")
        dispatches.add(item["dispatch_id"])
        owners.add(item["owner_id"])
    return value


def _indexed_dispatch(api: Any, head: str, dispatch_id: str) -> dict[str, Any]:
    # Dispatch data is resolved from immutable private intake, never from a
    # caller-supplied prompt/dispatch document.
    projects = git._read_json_at_commit(
        api, catalog.CATALOG_PATH, head, max_bytes=catalog.MAX_INDEX_BYTES
    )
    workflows = git._read_json_at_commit(
        api, catalog.WORKFLOWS_PATH, head, max_bytes=catalog.MAX_INDEX_BYTES
    )
    dispatches = git._read_json_at_commit(
        api, catalog.INDEX_PATH, head, max_bytes=catalog.MAX_INDEX_BYTES
    )
    if (projects is None or catalog.PROJECT_ID not in catalog.validate_projects(projects)
            or workflows is None
            or catalog.WORKFLOW_ID not in catalog.validate_workflows(workflows)
            or dispatches is None
            or dispatch_id not in catalog.validate_dispatch_index(dispatches)):
        raise PermissionError("Private parent dispatch is not indexed")
    dispatch = git._read_json_at_commit(
        api, catalog.dispatch_path(dispatch_id), head,
        max_bytes=catalog.MAX_RECORD_BYTES,
    )
    if dispatch is None:
        raise ValueError("Private parent indexed dispatch missing")
    dispatch_contract.validate_github_fabric_dispatch(dispatch)
    if dispatch["id"] != dispatch_id:
        raise ValueError("Private parent dispatch identity mismatch")
    return dispatch


def _snapshot(api: Any, dispatch_id: str) -> Snapshot:
    catalog.dispatch_path(dispatch_id)
    ref = api.request("GET", private_git.REF_PATH)
    if (not isinstance(ref, dict)
            or ref.get("ref") != "refs/heads/" + private_git.PRIVATE_BRANCH
            or ref.get("object", {}).get("type") != "commit"):
        raise ValueError("Private parent CAS origin ref invalid")
    head = git._require_sha(ref["object"].get("sha"), label="parent CAS origin")
    commit = api.request("GET", f"/git/commits/{head}")
    tree = git._require_sha(commit.get("tree", {}).get("sha"), label="parent CAS tree")
    dispatch = _indexed_dispatch(api, head, dispatch_id)
    identifier = policy.parent_id(dispatch["parent_conversation_url"])
    index = validate_index(git._read_json_at_commit(
        api, INDEX_PATH, head, max_bytes=MAX_INDEX_BYTES,
    ))
    parent = git._read_json_at_commit(
        api, policy.path(identifier), head, max_bytes=MAX_RECORD_BYTES,
    )
    history = git._read_json_at_commit(
        api, history_path(identifier), head, max_bytes=MAX_HISTORY_BYTES,
    )
    indexed = identifier in index["parent_ids"]
    if indexed != (parent is not None) or indexed != (history is not None):
        raise ValueError("Private parent CAS indexed record/history inconsistent")
    if parent is not None:
        policy.validate(parent)
        validate_history(history, identifier)
        if parent["id"] != identifier:
            raise ValueError("Private parent CAS record identity mismatch")
        last = history["entries"][-1]
        if (parent["fence_epoch"] != last["epoch"]
                or parent["dispatch_id"] != last["dispatch_id"]
                or parent["owner_id"] != last["owner_id"]):
            raise ValueError("Private parent CAS record does not match latest epoch")
        for entry in history["entries"]:
            witness = git._read_json_at_commit(
                api, epoch_path(identifier, entry["epoch"]), head,
                max_bytes=MAX_RECORD_BYTES,
            )
            if witness is None:
                raise ValueError("Private parent immutable epoch witness missing")
            policy.validate(witness)
            if (witness["id"] != identifier or witness["phase"] != "active"
                    or witness["fence_epoch"] != entry["epoch"]
                    or witness["dispatch_id"] != entry["dispatch_id"]
                    or witness["owner_id"] != entry["owner_id"]):
                raise ValueError("Private parent immutable epoch witness mismatch")
    return Snapshot(head, tree, dispatch, index, parent, history)


def _receipt_summary(api: Any, snapshot: Snapshot) -> dict[str, Any]:
    evidence: dict[str, dict[str, Any]] = {}
    for child in snapshot.dispatch["children"]:
        records: dict[str, Any] = {}
        for kind in ("claim", "ack", "result"):
            path = (f"{RECEIPT_ROOT}{snapshot.dispatch['id']}/{kind}/"
                    f"{child['request_id']}.json")
            record = git._read_json_at_commit(
                api, path, snapshot.head, max_bytes=MAX_RECEIPT_BYTES
            )
            if record is not None:
                records[kind] = record
        evidence[child["request_id"]] = records
    if snapshot.parent is None:
        raise PermissionError("Private parent receipt ownership missing")
    return {
        "source_head_sha": snapshot.head,
        **aggregate_owner_bound_private_child_receipts(
            snapshot.dispatch, evidence,
            owner_id=snapshot.parent["owner_id"],
        ),
    }


def _commit(api: Any, snapshot: Snapshot, writes: dict[str, dict[str, Any]]) -> None:
    if not 1 <= len(writes) <= 4:
        raise ValueError("Private parent CAS write count invalid")
    identifier = policy.parent_id(snapshot.dispatch["parent_conversation_url"])
    permitted = {
        INDEX_PATH, policy.path(identifier), history_path(identifier),
    }
    if any(name.startswith(EPOCH_ROOT) for name in writes):
        next_epoch = snapshot.parent["fence_epoch"] + 1 if snapshot.parent else 1
        permitted.add(epoch_path(identifier, next_epoch))
    if set(writes) - permitted:
        raise ValueError("Private parent CAS write paths outside namespace")
    entries = []
    for path, value in sorted(writes.items()):
        maximum = MAX_INDEX_BYTES if path == INDEX_PATH else (
            MAX_HISTORY_BYTES if path == history_path(identifier) else MAX_RECORD_BYTES
        )
        if path == INDEX_PATH:
            validate_index(value)
        elif path == history_path(identifier):
            validate_history(value, identifier)
        else:
            policy.validate(value)
        raw = json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8") + b"\n"
        if len(raw) > maximum:
            raise ValueError("Private parent CAS payload too large")
        blob = api.request("POST", "/git/blobs", {
            "content": raw.decode("utf-8"), "encoding": "utf-8"
        })
        entries.append({
            "path": path, "mode": "100644", "type": "blob",
            "sha": git._require_sha(blob.get("sha"), label="parent CAS blob"),
        })
    tree = api.request("POST", "/git/trees", {
        "base_tree": snapshot.tree, "tree": entries,
    })
    commit = api.request("POST", "/git/commits", {
        "message": "Record candidate private parent ownership (no Send authority)",
        "tree": git._require_sha(tree.get("sha"), label="parent CAS new tree"),
        "parents": [snapshot.head],
    })
    api.request("PATCH", private_git.REF_UPDATE_PATH, {
        "sha": git._require_sha(commit.get("sha"), label="parent CAS commit"),
        "force": False,
    })


def _confirmed_after_attempt(
    api: Any, dispatch_id: str, expected: dict[str, Any],
    *, expected_history: dict[str, Any] | None = None,
) -> ParentCandidateResult:
    try:
        current = _snapshot(api, dispatch_id)
    except Exception as exc:
        raise ParentCandidateWriteUncertain(
            "Private parent CAS outcome unreadable; do not repeat"
        ) from exc
    if (current.parent == expected
            and (expected_history is None or current.history == expected_history)):
        return ParentCandidateResult(
            "converged", current.head, expected["id"], expected["fence_epoch"]
        )
    raise ParentCandidateConflict(
        "Private parent CAS did not converge; no automatic retry or takeover"
    )


def _publish(
    api: Any, snapshot: Snapshot, expected: dict[str, Any],
    writes: dict[str, dict[str, Any]],
    *, history: dict[str, Any] | None = None,
) -> ParentCandidateResult:
    try:
        _commit(api, snapshot, writes)
    except git.GithubFabricHTTPError as exc:
        if exc.status not in {409, 422}:
            raise
        return _confirmed_after_attempt(
            api, snapshot.dispatch["id"], expected, expected_history=history
        )
    except git.GithubFabricTransportError:
        # The branch update may have succeeded. Inspect once; never blindly
        # repeat a GitHub side effect after an ambiguous acknowledgement.
        return _confirmed_after_attempt(
            api, snapshot.dispatch["id"], expected, expected_history=history
        )
    # Even a 200 ref response is checked against a freshly pinned snapshot.
    confirmed = _confirmed_after_attempt(
        api, snapshot.dispatch["id"], expected, expected_history=history
    )
    return ParentCandidateResult(
        "created", confirmed.source_head_sha, expected["id"], expected["fence_epoch"]
    )


def acquire_parent_candidate(
    dispatch_id: str, owner_id: str, *,
    enabled: bool = False, writer_authorized: bool = False,
    token: str | None = None, api: Any | None = None,
) -> ParentCandidateResult:
    if enabled is not True or writer_authorized is not True:
        raise PermissionError("Private parent CAS writer default-disabled")
    catalog.dispatch_path(dispatch_id)
    if api is None:
        api = private_git.PrivateFabricREST(token)
    snapshot = _snapshot(api, dispatch_id)
    identifier = policy.parent_id(snapshot.dispatch["parent_conversation_url"])
    # Historical identity denial precedes current-owner policy: even if a
    # newer epoch is active, an older dispatch can never be queued again.
    exact_active_replay = (
        snapshot.parent is not None and snapshot.parent["phase"] == "active"
        and snapshot.parent["dispatch_id"] == dispatch_id
        and snapshot.parent["owner_id"] == owner_id
    )
    if (not exact_active_replay and snapshot.history is not None
            and any(item["dispatch_id"] == dispatch_id or item["owner_id"] == owner_id
                    for item in snapshot.history["entries"])):
        raise PermissionError("Private parent historic dispatch/owner replay denied")
    operation, candidate = policy.propose_acquisition(
        snapshot.dispatch, owner_id, existing=snapshot.parent,
    )
    if operation == "replay":
        return ParentCandidateResult(
            "replay", snapshot.head, identifier, candidate["fence_epoch"]
        )

    # Until the ledger has an immutable per-epoch archive and a single
    # atomic archive/reset operation, a fresh parent epoch may NEVER inherit
    # another epoch's effect file at this per-parent path. Even a completed
    # previous owner may have had an uncertain Send recorded here. Preserve
    # evidence and fail closed; do not overwrite or infer that it is safe.
    if git._read_json_at_commit(
        api, effects.effect_path(identifier), snapshot.head,
        max_bytes=effects.MAX_RECORD_BYTES,
    ) is not None:
        raise PermissionError(
            "Private parent effect ledger requires epoch archival before takeover"
        )
    # An archive-shaped file is not an atomic transfer witness. Even when
    # the old active ledger has been removed out of band, do not infer that
    # its Send effects were globally fenced or that takeover was allowed.
    # This remains a mandatory deny until a versioned transition marker
    # and all browser drivers participate in the same protocol.
    # Check ALL possible archive keys, not only indexed historical
    # epochs: a missing/corrupt parent index cannot prove that an orphaned
    # archive from an earlier controller is harmless. An unindexed future
    # archive is also a conflict. This conservative candidate performs at
    # most MAX_EPOCHS bounded, read-only lookups at the SAME pinned HEAD.
    # Never use the absence of an archive as Send permission.
    for prior_epoch in range(1, MAX_EPOCHS + 1):
        if git._read_json_at_commit(
            api,
            effect_archives.archive_path(identifier, prior_epoch),
            snapshot.head,
            max_bytes=effect_archives.MAX_ARCHIVE_BYTES,
        ) is not None:
            raise PermissionError(
                "Private parent effect archive is not a takeover permit"
            )
    if snapshot.history is not None and len(snapshot.history["entries"]) >= MAX_EPOCHS:
        raise ValueError("Private parent epoch capacity reached; manual review required")
    next_history = {
        "schema_version": 1, "parent_id": identifier,
        "entries": [
            *(snapshot.history["entries"] if snapshot.history else []),
            {
                "epoch": candidate["fence_epoch"],
                "dispatch_id": candidate["dispatch_id"],
                "owner_id": candidate["owner_id"],
            },
        ],
    }
    validate_history(next_history, identifier)
    witness_path = epoch_path(identifier, candidate["fence_epoch"])
    if git._read_json_at_commit(
        api, witness_path, snapshot.head, max_bytes=MAX_RECORD_BYTES
    ) is not None:
        raise ValueError("Private parent new epoch witness already exists")
    new_index = validate_index({
        "schema_version": 1,
        "parent_ids": sorted({*snapshot.index["parent_ids"], identifier}),
    })
    writes = {
        policy.path(identifier): candidate,
        history_path(identifier): next_history,
        witness_path: candidate,
    }
    if new_index != snapshot.index:
        writes[INDEX_PATH] = new_index
    return _publish(api, snapshot, candidate, writes, history=next_history)


def change_parent_candidate(
    dispatch_id: str, owner_id: str, epoch: int, action: str, *,
    enabled: bool = False, writer_authorized: bool = False,
    token: str | None = None, api: Any | None = None,
) -> ParentCandidateResult:
    if enabled is not True or writer_authorized is not True:
        raise PermissionError("Private parent CAS state change default-disabled")
    if action not in {"complete", "freeze_unknown"}:
        raise ValueError("Private parent CAS state change invalid")
    catalog.dispatch_path(dispatch_id)
    if api is None:
        api = private_git.PrivateFabricREST(token)
    snapshot = _snapshot(api, dispatch_id)
    current = snapshot.parent
    if (current is None or current["dispatch_id"] != dispatch_id
            or current["owner_id"] != owner_id
            or type(epoch) is not int or current["fence_epoch"] != epoch):
        raise PermissionError("Private parent CAS owner/epoch mismatch")
    if action == "complete":
        if current["phase"] == "completed":
            return ParentCandidateResult(
                "replay", snapshot.head, current["id"], epoch
            )
        expected = policy.propose_completion(
            current, snapshot.dispatch, _receipt_summary(api, snapshot)
        )
    else:
        if current["phase"] == "unknown_frozen":
            return ParentCandidateResult(
                "replay", snapshot.head, current["id"], epoch
            )
        expected = policy.freeze_uncertain(current)
    return _publish(api, snapshot, expected, {policy.path(current["id"]): expected})
