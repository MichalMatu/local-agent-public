"""Read-only, same-commit child-authored GitHub-first result records.

The browser never creates or reads an assistant answer. A GitHub Connector
write by the child produces this fixed private path; Local Agent validates
identity and returns results to the parent through GitHub.
"""

from __future__ import annotations

from typing import Any

from local_agent.conversation import github_fabric_dispatch as dispatch_contract
from local_agent.conversation import github_fabric_github as git

MAX_RESULT_BYTES = 8192


def result_path(dispatch: dict[str, Any], child: dict[str, Any]) -> str:
    dispatch_contract.validate_github_fabric_dispatch(dispatch)
    if child not in dispatch["children"]:
        raise ValueError("Child not in immutable dispatch")
    return ("projects/local-agent/workflows/workflow-001/results/"
            + dispatch["id"] + "/" + child["request_id"] + ".json")


def validate_result(dispatch: dict[str, Any], child: dict[str, Any], record: Any) -> str:
    if (not isinstance(record, dict) or set(record) != {
        "schema_version", "kind", "dispatch_id", "child_request_id",
        "spawn_transaction_id", "status", "result_text",
    } or type(record["schema_version"]) is not int or record["schema_version"] != 1
            or record["kind"] != "child_agent_result"
            or record["dispatch_id"] != dispatch["id"]
            or record["child_request_id"] != child["request_id"]
            or record["spawn_transaction_id"] != child["spawn"]["transaction_id"]
            or record["status"] != "completed"
            or not isinstance(record["result_text"], str)
            or not record["result_text"].strip()
            or len(record["result_text"]) > 6000):
        raise ValueError("GitHub-first child result identity/content invalid")
    return record["result_text"]


def read_pinned_results(api: Any, head: str, dispatch: dict[str, Any]) -> dict[str, str]:
    """Read only indexed-dispatch child paths at one Git commit SHA."""
    git._require_sha(head, label="result source head")
    dispatch_contract.validate_github_fabric_dispatch(dispatch)
    result = {}
    for child in dispatch["children"]:
        record = git._read_json_at_commit(api, result_path(dispatch, child), head,
                                          max_bytes=MAX_RESULT_BYTES)
        if record is not None:
            result[child["request_id"]] = validate_result(dispatch, child, record)
    return result
