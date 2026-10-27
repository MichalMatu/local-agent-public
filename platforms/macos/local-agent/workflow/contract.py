from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from typing import Any

from local_agent.repository.binding import canonical_agent_binding
from local_agent.runtime.task_contract import validate_task
from local_agent.workflow import methods

WORKFLOW_SCHEMA_VERSION = 1
MAX_WORKFLOW_FILE_BYTES = 2 * 1024 * 1024
MAX_WORKFLOW_NODES = 64
MAX_NODE_DEPENDENCIES = 16
MAX_WORKFLOW_ID_CHARS = 200
MAX_NODE_ID_CHARS = 200
MAX_REPOSITORY_ID_CHARS = 200
MAX_GATE_PROMPT_CHARS = 4096
MAX_GATE_CHOICES = 16
MAX_GATE_CHOICE_CHARS = 100

WORKFLOW_NODE_KINDS = frozenset(
    {
        "task",
        "reasoning",
        "barrier",
        "user_gate",
        "planner_checkpoint",
    }
)
WORKFLOW_METHOD_MODES = frozenset({"static", "adaptive"})

_ID_RE = re.compile(r"^[A-Za-z0-9._-]+$")
_REPOSITORY_ID_RE = re.compile(r"^[A-Za-z0-9._-]+$")
_GATE_CHOICE_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")


def _canonical_bytes(payload: Any) -> bytes:
    try:
        text = json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("workflow manifest must be canonical JSON data") from exc
    return text.encode("utf-8")


def _validate_id(value: Any, *, field: str, maximum: int) -> str:
    if not isinstance(value, str) or not value or len(value) > maximum:
        raise ValueError(f"{field} must be a non-empty string up to {maximum} characters")
    if not _ID_RE.fullmatch(value):
        raise ValueError(f"{field} contains unsupported characters")
    return value


def validate_workflow_id(value: Any) -> str:
    return _validate_id(
        value,
        field="workflow id",
        maximum=MAX_WORKFLOW_ID_CHARS,
    )


def _validate_repository_id(value: Any) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > MAX_REPOSITORY_ID_CHARS
        or not _REPOSITORY_ID_RE.fullmatch(value)
    ):
        raise ValueError("repository_id must be a bounded canonical repository id")
    return value


def _validate_created_at(value: Any) -> str:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise ValueError("created_at must be an RFC3339 UTC timestamp ending in Z")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise ValueError("created_at must be a valid RFC3339 UTC timestamp") from exc
    if parsed.tzinfo != timezone.utc:
        raise ValueError("created_at must use UTC")
    return value


def _validate_method_mode(manifest: dict[str, Any]) -> str:
    if "method_mode" in manifest and "method" not in manifest:
        raise ValueError("workflow method_mode requires a method reference")
    mode = manifest.get("method_mode", "static")
    if not isinstance(mode, str) or mode not in WORKFLOW_METHOD_MODES:
        raise ValueError(
            f"workflow method_mode must be one of {sorted(WORKFLOW_METHOD_MODES)!r}"
        )
    return mode


def _validate_dependencies(node: dict[str, Any]) -> tuple[str, ...]:
    raw = node.get("depends_on")
    if not isinstance(raw, list):
        raise ValueError(f"workflow node {node.get('id')!r} depends_on must be a list")
    if len(raw) > MAX_NODE_DEPENDENCIES:
        raise ValueError(
            f"workflow node {node.get('id')!r} depends_on exceeds "
            f"{MAX_NODE_DEPENDENCIES} items"
        )
    dependencies: list[str] = []
    seen: set[str] = set()
    for item in raw:
        dependency = _validate_id(
            item,
            field=f"workflow node {node.get('id')!r} dependency",
            maximum=MAX_NODE_ID_CHARS,
        )
        if dependency in seen:
            raise ValueError(
                f"workflow node {node.get('id')!r} has duplicate dependency {dependency!r}"
            )
        seen.add(dependency)
        dependencies.append(dependency)
    return tuple(dependencies)


def _validate_task_node(node: dict[str, Any]) -> None:
    allowed = {
        "id",
        "kind",
        "phase",
        "repository_id",
        "agent_binding",
        "depends_on",
        "task",
    }
    extra = set(node) - allowed
    if extra:
        raise ValueError(
            f"workflow task node {node['id']!r} contains unsupported fields: {sorted(extra)!r}"
        )
    _validate_repository_id(node.get("repository_id"))
    binding = canonical_agent_binding(
        node.get("agent_binding"),
        field=f"workflow node {node['id']!r} agent_binding",
    )
    task = node.get("task")
    if not isinstance(task, dict):
        raise ValueError(f"workflow task node {node['id']!r} task must be an object")
    if "id" in task:
        raise ValueError(
            f"workflow task node {node['id']!r} must not define task id; coordinator owns child identity"
        )
    if "agent_binding" in task:
        raise ValueError(
            f"workflow task node {node['id']!r} must not define agent_binding; node identity is authoritative"
        )
    if "workflow" in task:
        raise ValueError(
            f"workflow task node {node['id']!r} must not define workflow provenance; coordinator owns it"
        )
    candidate = dict(task)
    candidate["id"] = "workflow-contract-child"
    candidate["agent_binding"] = binding
    validate_task(candidate, require_agent_binding=True)


def _validate_reasoning_node(node: dict[str, Any]) -> None:
    allowed = {
        "id",
        "kind",
        "phase",
        "repository_id",
        "agent_binding",
        "depends_on",
    }
    extra = set(node) - allowed
    if extra:
        raise ValueError(
            f"workflow reasoning node {node['id']!r} contains unsupported fields: {sorted(extra)!r}"
        )
    _validate_repository_id(node.get("repository_id"))
    canonical_agent_binding(
        node.get("agent_binding"),
        field=f"workflow node {node['id']!r} agent_binding",
    )


def _validate_barrier_node(node: dict[str, Any]) -> None:
    allowed = {"id", "kind", "phase", "depends_on"}
    extra = set(node) - allowed
    if extra:
        raise ValueError(
            f"workflow barrier node {node['id']!r} contains unsupported fields: {sorted(extra)!r}"
        )


def _validate_checkpoint_node(node: dict[str, Any]) -> None:
    allowed = {"id", "kind", "phase", "depends_on"}
    extra = set(node) - allowed
    if extra:
        raise ValueError(
            f"workflow planner_checkpoint node {node['id']!r} contains unsupported fields: "
            f"{sorted(extra)!r}"
        )


def _validate_user_gate_node(node: dict[str, Any]) -> None:
    allowed = {"id", "kind", "phase", "depends_on", "prompt", "choices"}
    extra = set(node) - allowed
    if extra:
        raise ValueError(
            f"workflow user_gate node {node['id']!r} contains unsupported fields: {sorted(extra)!r}"
        )
    prompt = node.get("prompt")
    if (
        not isinstance(prompt, str)
        or not prompt.strip()
        or len(prompt) > MAX_GATE_PROMPT_CHARS
    ):
        raise ValueError("user_gate prompt must be a non-empty bounded string")
    choices = node.get("choices")
    if not isinstance(choices, list) or not choices:
        raise ValueError("user_gate choices must be a non-empty list")
    if len(choices) > MAX_GATE_CHOICES:
        raise ValueError(f"user_gate choices exceeds {MAX_GATE_CHOICES} items")
    seen: set[str] = set()
    for raw in choices:
        if (
            not isinstance(raw, str)
            or not raw
            or len(raw) > MAX_GATE_CHOICE_CHARS
            or raw != raw.strip()
            or raw != raw.casefold()
            or not _GATE_CHOICE_RE.fullmatch(raw)
        ):
            raise ValueError("user_gate choices must be canonical lowercase identifiers")
        if raw in seen:
            raise ValueError("user_gate choices must be unique")
        seen.add(raw)


def _validate_acyclic(nodes: list[dict[str, Any]]) -> None:
    dependencies = {
        str(node["id"]): tuple(str(item) for item in node["depends_on"])
        for node in nodes
    }
    incoming = {node_id: len(items) for node_id, items in dependencies.items()}
    children: dict[str, list[str]] = {node_id: [] for node_id in dependencies}
    for node_id, items in dependencies.items():
        for dependency in items:
            children[dependency].append(node_id)

    ready = [node_id for node_id, count in incoming.items() if count == 0]
    visited = 0
    while ready:
        current = ready.pop()
        visited += 1
        for child in children[current]:
            incoming[child] -= 1
            if incoming[child] == 0:
                ready.append(child)
    if visited != len(nodes):
        raise ValueError("workflow dependency graph contains a cycle")


def validate_workflow_manifest(manifest: dict[str, Any]) -> None:
    if not isinstance(manifest, dict):
        raise ValueError("workflow manifest must be an object")
    if len(_canonical_bytes(manifest)) > MAX_WORKFLOW_FILE_BYTES:
        raise ValueError(f"workflow manifest exceeds {MAX_WORKFLOW_FILE_BYTES} bytes")

    allowed = {
        "schema_version",
        "id",
        "created_at",
        "method",
        "method_mode",
        "nodes",
    }
    extra = set(manifest) - allowed
    if extra:
        raise ValueError(f"workflow manifest contains unsupported fields: {sorted(extra)!r}")
    if type(manifest.get("schema_version")) is not int or manifest["schema_version"] != WORKFLOW_SCHEMA_VERSION:
        raise ValueError(f"workflow schema_version must be {WORKFLOW_SCHEMA_VERSION}")
    validate_workflow_id(manifest.get("id"))
    _validate_created_at(manifest.get("created_at"))
    method_mode = _validate_method_mode(manifest)

    method_spec: dict[str, Any] | None = None
    if "method" in manifest:
        reference = manifest["method"]
        methods.validate_method_reference(reference)
        method_spec = methods.load_builtin_method(
            reference["name"],
            version=reference["version"],
        )
        methods.require_method_match(reference, method_spec)

    nodes = manifest.get("nodes")
    if not isinstance(nodes, list) or not nodes:
        raise ValueError("workflow nodes must be a non-empty list")
    if len(nodes) > MAX_WORKFLOW_NODES:
        raise ValueError(f"workflow nodes exceeds {MAX_WORKFLOW_NODES} items")

    node_ids: set[str] = set()
    dependencies_by_id: dict[str, tuple[str, ...]] = {}
    for raw_node in nodes:
        if not isinstance(raw_node, dict):
            raise ValueError("workflow nodes must be objects")
        node_id = _validate_id(
            raw_node.get("id"),
            field="workflow node id",
            maximum=MAX_NODE_ID_CHARS,
        )
        if node_id in node_ids:
            raise ValueError(f"duplicate workflow node id: {node_id!r}")
        node_ids.add(node_id)
        kind = raw_node.get("kind")
        if not isinstance(kind, str) or kind not in WORKFLOW_NODE_KINDS:
            raise ValueError(f"unsupported workflow node kind: {kind!r}")
        if "phase" in raw_node:
            methods.validate_phase_name(raw_node["phase"])
        dependencies_by_id[node_id] = _validate_dependencies(raw_node)

        if kind == "task":
            _validate_task_node(raw_node)
        elif kind == "reasoning":
            _validate_reasoning_node(raw_node)
        elif kind == "barrier":
            _validate_barrier_node(raw_node)
        elif kind == "user_gate":
            _validate_user_gate_node(raw_node)
        elif kind == "planner_checkpoint":
            _validate_checkpoint_node(raw_node)

    for node_id, dependencies in dependencies_by_id.items():
        for dependency in dependencies:
            if dependency not in node_ids:
                raise ValueError(
                    f"workflow node {node_id!r} has unknown dependency {dependency!r}"
                )

    _validate_acyclic(nodes)
    if method_spec is not None:
        if method_mode == "adaptive":
            methods.validate_adaptive_workflow_method(manifest, method_spec)
        else:
            methods.validate_workflow_method(manifest, method_spec)


def validate_complete_workflow_manifest(manifest: dict[str, Any]) -> None:
    """Require an admitted manifest to satisfy its complete method contract.

    Static workflows already satisfy this during normal admission. Adaptive workflows
    call this on the effective base+revision graph before the lineage can be considered
    method-complete.
    """
    validate_workflow_manifest(manifest)
    if "method" not in manifest:
        return
    reference = manifest["method"]
    spec = methods.load_builtin_method(
        reference["name"],
        version=reference["version"],
    )
    methods.validate_workflow_method(manifest, spec)


def manifest_digest(manifest: dict[str, Any]) -> str:
    validate_workflow_manifest(manifest)
    return "sha256:" + hashlib.sha256(_canonical_bytes(manifest)).hexdigest()
