from __future__ import annotations

from typing import Any

from local_agent.workflow.contract import validate_workflow_manifest

NODE_STATES = frozenset(
    {
        "pending",
        "blocked_dependency",
        "ready",
        "dispatched",
        "running",
        "succeeded",
        "failed",
        "cancelled",
        "waiting_user",
        "waiting_planner",
        "waiting_conversation",
        "blocked_interrupted",
    }
)

WORKFLOW_STATES = frozenset(
    {
        "pending",
        "running",
        "waiting_user",
        "waiting_planner",
        "waiting_conversation",
        "failed",
        "cancelled",
        "completed",
    }
)

_ALLOWED_TRANSITIONS: dict[str, frozenset[str]] = {
    "pending": frozenset(
        {
            "blocked_dependency",
            "ready",
            "succeeded",
            "waiting_user",
            "waiting_planner",
            "waiting_conversation",
            "cancelled",
        }
    ),
    "blocked_dependency": frozenset(
        {
            "ready",
            "succeeded",
            "waiting_user",
            "waiting_planner",
            "waiting_conversation",
            "cancelled",
        }
    ),
    # A ready node may discover remote evidence from a publish that completed before
    # the coordinator process crashed. These recovery transitions avoid replaying the
    # child merely because the local state update did not happen.
    "ready": frozenset(
        {
            "dispatched",
            "running",
            "succeeded",
            "failed",
            "cancelled",
            "blocked_interrupted",
        }
    ),
    "dispatched": frozenset(
        {
            "running",
            "succeeded",
            "failed",
            "cancelled",
            "blocked_interrupted",
        }
    ),
    "running": frozenset(
        {
            "succeeded",
            "failed",
            "cancelled",
            "blocked_interrupted",
        }
    ),
    "waiting_user": frozenset({"succeeded", "cancelled"}),
    "waiting_planner": frozenset({"succeeded", "cancelled"}),
    "waiting_conversation": frozenset({"succeeded", "failed", "cancelled"}),
    "succeeded": frozenset(),
    "failed": frozenset(),
    "cancelled": frozenset(),
    "blocked_interrupted": frozenset(),
}

_DEPENDENCY_SUCCESS = "succeeded"
_DEPENDENCY_BLOCKING_TERMINAL = frozenset(
    {"failed", "cancelled", "blocked_interrupted"}
)


def _validate_node_state(value: str) -> str:
    if not isinstance(value, str) or value not in NODE_STATES:
        raise ValueError(f"unsupported workflow node state: {value!r}")
    return value


def _manifest_nodes(manifest: dict[str, Any]) -> dict[str, dict[str, Any]]:
    validate_workflow_manifest(manifest)
    return {str(node["id"]): node for node in manifest["nodes"]}


def _validate_state_map(
    manifest: dict[str, Any],
    states: dict[str, str],
) -> dict[str, dict[str, Any]]:
    nodes = _manifest_nodes(manifest)
    if not isinstance(states, dict):
        raise ValueError("workflow node states must be an object")
    if set(states) != set(nodes):
        missing = sorted(set(nodes) - set(states))
        extra = sorted(set(states) - set(nodes))
        raise ValueError(
            f"workflow node states do not match manifest: missing={missing!r} extra={extra!r}"
        )
    for value in states.values():
        _validate_node_state(value)
    return nodes


def transition_node_state(current: str, target: str) -> str:
    current = _validate_node_state(current)
    target = _validate_node_state(target)
    if current == target:
        return current
    if target not in _ALLOWED_TRANSITIONS[current]:
        raise ValueError(f"invalid workflow node transition: {current} -> {target}")
    return target


def _dependency_target(kind: str) -> str:
    if kind == "task":
        return "ready"
    if kind == "reasoning":
        return "waiting_conversation"
    if kind == "barrier":
        return "succeeded"
    if kind == "user_gate":
        return "waiting_user"
    if kind == "planner_checkpoint":
        return "waiting_planner"
    raise ValueError(f"unsupported workflow node kind: {kind!r}")


def initial_node_states(manifest: dict[str, Any]) -> dict[str, str]:
    nodes = _manifest_nodes(manifest)
    states = {node_id: "pending" for node_id in nodes}
    return advance_dependency_states(manifest, states)


def advance_dependency_states(
    manifest: dict[str, Any],
    states: dict[str, str],
) -> dict[str, str]:
    nodes = _validate_state_map(manifest, states)
    updated = dict(states)

    changed = True
    while changed:
        changed = False
        for node_id, node in nodes.items():
            current = updated[node_id]
            if current not in {"pending", "blocked_dependency"}:
                continue
            dependencies = tuple(str(item) for item in node["depends_on"])
            dependency_states = [updated[item] for item in dependencies]
            if any(
                value in _DEPENDENCY_BLOCKING_TERMINAL
                for value in dependency_states
            ):
                target = "blocked_dependency"
            elif all(value == _DEPENDENCY_SUCCESS for value in dependency_states):
                target = _dependency_target(str(node["kind"]))
            else:
                target = "blocked_dependency"
            if target != current:
                updated[node_id] = transition_node_state(current, target)
                changed = True
    return updated


def workflow_state(states: dict[str, str]) -> str:
    if not isinstance(states, dict) or not states:
        raise ValueError("workflow node states must be a non-empty object")
    values = tuple(_validate_node_state(value) for value in states.values())

    if "failed" in values:
        return "failed"
    if "blocked_interrupted" in values or "waiting_planner" in values:
        return "waiting_planner"
    if "waiting_user" in values:
        return "waiting_user"
    # A conversation wait is local to one reasoning node. Independent executable
    # work must remain visible as running instead of being masked by that wait.
    if any(value in {"ready", "dispatched", "running"} for value in values):
        return "running"
    if "waiting_conversation" in values:
        return "waiting_conversation"
    if all(value == "succeeded" for value in values):
        return "completed"
    if "cancelled" in values:
        return "cancelled"
    if "succeeded" in values:
        return "running"
    return "pending"
