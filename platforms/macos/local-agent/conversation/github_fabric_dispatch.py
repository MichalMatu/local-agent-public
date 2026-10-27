"""GitHub-first Conversation Fabric browser-dispatch contract.

Local Agent produces this immutable envelope after semantic request admission.
Chat Bridge may validate/project it into browser effects, but the envelope carries
no repository execution authority and no runtime Chrome tab identity.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Iterable

from local_agent.conversation import bootstrap, contract, operator_contract, spawn

GITHUB_FABRIC_DISPATCH_SCHEMA_VERSION = 1
BROWSER_SPAWN_SCHEMA_VERSION = 1
MAX_GITHUB_FABRIC_CHILDREN = 4
MAX_BROWSER_BOOTSTRAP_CHARS = 32_768
MAX_GITHUB_FABRIC_DISPATCH_BYTES = 128 * 1024

_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,119}$")
_CHILD_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
_CAMPAIGN_ID_RE = re.compile(r"^cf-[0-9a-f]{16}$")
_TRANSACTION_RE = re.compile(r"^spawn-[0-9a-f]{64}$")
_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")

_DISPATCH_FIELDS = frozenset(
    {
        "schema_version",
        "operation",
        "id",
        "request_id",
        "request_digest",
        "campaign_id",
        "parent_conversation_url",
        "children",
    }
)
_CHILD_FIELDS = frozenset({"id", "request_id", "role", "spawn"})
_SPAWN_FIELDS = frozenset(
    {
        "schema_version",
        "transaction_id",
        "child_request_digest",
        "bootstrap_digest",
        "bootstrap_text",
    }
)


def _canonical_bytes(value: Any) -> bytes:
    try:
        text = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("GitHub Fabric dispatch must be canonical JSON data") from exc
    return text.encode("utf-8")


def _exact_fields(value: dict[str, Any], expected: frozenset[str], *, field: str) -> None:
    if set(value) != expected:
        raise ValueError(f"{field} fields do not match schema")


def _browser_child_id(request_id: str) -> str:
    if _CHILD_ID_RE.fullmatch(request_id):
        return request_id
    digest = hashlib.sha256(request_id.encode("utf-8")).hexdigest()
    return f"child-{digest[:16]}"


def _browser_bootstrap(request: dict[str, Any], child_id: str,
                       dispatch_id: str) -> str:
    """Child publishes its own result through GitHub, never an observed DOM reply."""
    transaction_id = spawn.spawn_transaction_id(request, 1)
    result_path = (
        f"projects/local-agent/workflows/workflow-001/results/"
        f"{dispatch_id}/{request['id']}.json"
    )
    result_example = {
        "schema_version": 1,
        "kind": "child_agent_result",
        "dispatch_id": dispatch_id,
        "child_request_id": request["id"],
        "spawn_transaction_id": transaction_id,
        "status": "completed",
        "result_text": "REPLACE WITH YOUR FINAL RESULT (MAX 6000 CHARACTERS)",
    }
    suffix = (
        "\nGitHub-first completion contract (mandatory):\n"
        "When finished, publish exactly one UTF-8 JSON result file through your "
        "connected GitHub Connector to the private repository "
        "example-owner/local-agent-fabric-private, branch fabric-data.\n"
        f"Path: {result_path}\n"
        "Use the following object as a template, replacing only result_text "
        "with your actual bounded final result:\n"
        + json.dumps(result_example, ensure_ascii=False, sort_keys=True) + "\n"
        "Create once; never overwrite a conflicting existing result. "
        "If GitHub publication is unavailable, report BLOCKED explicitly and "
        "do not claim completion. Chat Bridge will not read your answer from "
        "the browser page. Do not print repository credentials.\n"
    )
    return bootstrap.child_bootstrap_message(request) + suffix


def _campaign_id(operator_request: dict[str, Any], request_digest: str) -> str:
    identity = _canonical_bytes(
        [
            "github-fabric-campaign-v1",
            operator_request["id"],
            request_digest,
            operator_request["parent_conversation_url"],
        ]
    )
    return "cf-" + hashlib.sha256(identity).hexdigest()[:16]


def _dispatch_id(request_digest: str) -> str:
    identity = _canonical_bytes(["github-fabric-dispatch-v1", request_digest])
    return "fabric-" + hashlib.sha256(identity).hexdigest()[:32]


def validate_github_fabric_dispatch(dispatch: dict[str, Any]) -> None:
    if not isinstance(dispatch, dict):
        raise ValueError("GitHub Fabric dispatch must be an object")
    if len(_canonical_bytes(dispatch)) > MAX_GITHUB_FABRIC_DISPATCH_BYTES:
        raise ValueError(
            f"GitHub Fabric dispatch exceeds {MAX_GITHUB_FABRIC_DISPATCH_BYTES} bytes"
        )
    _exact_fields(dispatch, _DISPATCH_FIELDS, field="GitHub Fabric dispatch")
    if dispatch.get("schema_version") != GITHUB_FABRIC_DISPATCH_SCHEMA_VERSION:
        raise ValueError("GitHub Fabric dispatch schema_version mismatch")
    if dispatch.get("operation") != "delegate":
        raise ValueError("GitHub Fabric dispatch operation must be delegate")
    if not isinstance(dispatch.get("id"), str) or not _ID_RE.fullmatch(dispatch["id"]):
        raise ValueError("GitHub Fabric dispatch id is invalid")
    if not isinstance(dispatch.get("request_id"), str) or not _ID_RE.fullmatch(
        dispatch["request_id"]
    ):
        raise ValueError("GitHub Fabric request_id is invalid")
    if not isinstance(dispatch.get("request_digest"), str) or not _DIGEST_RE.fullmatch(
        dispatch["request_digest"]
    ):
        raise ValueError("GitHub Fabric request_digest is invalid")
    if not isinstance(dispatch.get("campaign_id"), str) or not _CAMPAIGN_ID_RE.fullmatch(
        dispatch["campaign_id"]
    ):
        raise ValueError("GitHub Fabric campaign_id is invalid")
    parent = contract.canonical_conversation_url(dispatch.get("parent_conversation_url"))
    if parent != dispatch["parent_conversation_url"]:
        raise ValueError("GitHub Fabric parent_conversation_url must be canonical")

    children = dispatch.get("children")
    if not isinstance(children, list) or not 1 <= len(children) <= MAX_GITHUB_FABRIC_CHILDREN:
        raise ValueError(
            f"GitHub Fabric children must contain 1..{MAX_GITHUB_FABRIC_CHILDREN} items"
        )

    child_ids: set[str] = set()
    request_ids: set[str] = set()
    transactions: set[str] = set()
    for child in children:
        if not isinstance(child, dict):
            raise ValueError("GitHub Fabric child must be an object")
        _exact_fields(child, _CHILD_FIELDS, field="GitHub Fabric child")
        child_id = child.get("id")
        request_id = child.get("request_id")
        if (
            not isinstance(child_id, str)
            or not _CHILD_ID_RE.fullmatch(child_id)
            or child_id in child_ids
        ):
            raise ValueError("GitHub Fabric child id is invalid or duplicated")
        if (
            not isinstance(request_id, str)
            or not _ID_RE.fullmatch(request_id)
            or request_id in request_ids
        ):
            raise ValueError("GitHub Fabric child request_id is invalid or duplicated")
        if child.get("role") not in contract.CHILD_ROLES:
            raise ValueError("GitHub Fabric child role is invalid")
        raw_spawn = child.get("spawn")
        if not isinstance(raw_spawn, dict):
            raise ValueError("GitHub Fabric child spawn must be an object")
        _exact_fields(raw_spawn, _SPAWN_FIELDS, field="GitHub Fabric child spawn")
        if raw_spawn.get("schema_version") != BROWSER_SPAWN_SCHEMA_VERSION:
            raise ValueError("GitHub Fabric spawn schema_version mismatch")
        transaction_id = raw_spawn.get("transaction_id")
        if (
            not isinstance(transaction_id, str)
            or not _TRANSACTION_RE.fullmatch(transaction_id)
            or transaction_id in transactions
        ):
            raise ValueError("GitHub Fabric spawn transaction_id is invalid or duplicated")
        for key in ("child_request_digest", "bootstrap_digest"):
            value = raw_spawn.get(key)
            if not isinstance(value, str) or not _DIGEST_RE.fullmatch(value):
                raise ValueError(f"GitHub Fabric spawn {key} is invalid")
        bootstrap_text = raw_spawn.get("bootstrap_text")
        if (
            not isinstance(bootstrap_text, str)
            or not bootstrap_text.strip()
            or len(bootstrap_text) > MAX_BROWSER_BOOTSTRAP_CHARS
        ):
            raise ValueError(
                "GitHub Fabric spawn bootstrap_text must be non-empty bounded text"
            )
        child_ids.add(child_id)
        request_ids.add(request_id)
        transactions.add(transaction_id)


def build_github_fabric_dispatch(
    operator_request: dict[str, Any],
    child_requests: Iterable[dict[str, Any]],
) -> dict[str, Any]:
    """Build one immutable browser dispatch from already-admitted child requests."""

    operator_contract.validate_operator_request(operator_request)
    requests = list(child_requests)
    by_id: dict[str, dict[str, Any]] = {}
    for request in requests:
        contract.validate_child_request(request)
        request_id = str(request["id"])
        if request_id in by_id:
            raise ValueError(f"duplicate admitted child request: {request_id!r}")
        by_id[request_id] = request

    expected_ids = [str(child["request_id"]) for child in operator_request["children"]]
    if set(by_id) != set(expected_ids) or len(by_id) != len(expected_ids):
        raise ValueError("admitted child requests do not match operator request")

    request_digest = operator_contract.operator_request_digest(operator_request)
    dispatch_id = _dispatch_id(request_digest)
    children: list[dict[str, Any]] = []
    for spec in operator_request["children"]:
        request = by_id[str(spec["request_id"])]
        for actual, expected, label in (
            (request["workflow_id"], operator_request["workflow_id"], "workflow_id"),
            (
                request["parent_conversation_url"],
                operator_request["parent_conversation_url"],
                "parent_conversation_url",
            ),
            (request["workflow_node_id"], spec["node_id"], "workflow_node_id"),
            (request["role"], spec["role"], "role"),
        ):
            if actual != expected:
                raise ValueError(f"admitted child request {label} does not match operator request")

        child_id = _browser_child_id(str(spec["request_id"]))
        bootstrap_text = _browser_bootstrap(request, child_id, dispatch_id)
        if len(bootstrap_text) > MAX_BROWSER_BOOTSTRAP_CHARS:
            raise ValueError(
                "admitted child bootstrap exceeds browser dispatch character bound"
            )
        children.append(
            {
                "id": child_id,
                "request_id": request["id"],
                "role": request["role"],
                "spawn": {
                    "schema_version": BROWSER_SPAWN_SCHEMA_VERSION,
                    "transaction_id": spawn.spawn_transaction_id(request, 1),
                    "child_request_digest": contract.child_request_digest(request),
                    "bootstrap_digest": "sha256:" + hashlib.sha256(
                        bootstrap_text.encode("utf-8")
                    ).hexdigest(),
                    "bootstrap_text": bootstrap_text,
                },
            }
        )

    dispatch = {
        "schema_version": GITHUB_FABRIC_DISPATCH_SCHEMA_VERSION,
        "operation": "delegate",
        "id": dispatch_id,
        "request_id": operator_request["id"],
        "request_digest": request_digest,
        "campaign_id": _campaign_id(operator_request, request_digest),
        "parent_conversation_url": operator_request["parent_conversation_url"],
        "children": children,
    }
    validate_github_fabric_dispatch(dispatch)
    return dispatch


def reconcile_github_fabric_dispatch(
    existing: dict[str, Any] | None,
    candidate: dict[str, Any],
) -> dict[str, Any]:
    validate_github_fabric_dispatch(candidate)
    if existing is None:
        return dict(candidate)
    validate_github_fabric_dispatch(existing)
    if existing["id"] != candidate["id"]:
        raise ValueError("GitHub Fabric dispatch id mismatch")
    if _canonical_bytes(existing) != _canonical_bytes(candidate):
        raise ValueError("GitHub Fabric same-id dispatch conflict")
    return dict(existing)
