from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

METHOD_SCHEMA_VERSION = 1
MAX_METHOD_FILE_BYTES = 256 * 1024
MAX_METHOD_NAME_CHARS = 100
MAX_METHOD_DESCRIPTION_CHARS = 4096
MAX_METHOD_PHASES = 32
MAX_METHOD_REQUIREMENTS = 64
BUILTIN_METHODS_DIR = Path(__file__).resolve().parent / "method_specs"
SUPPORTED_METHOD_REQUIREMENTS = frozenset(
    {
        "final_full_verification",
        "planner_checkpoint_after_audit",
        "multi_repository_implementation",
        "integration_joins_implementation",
        "final_review_after_integration",
    }
)

_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")


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
        raise ValueError("method payload must be canonical JSON data") from exc
    return text.encode("utf-8")


def _canonical_name(value: Any, *, field: str) -> str:
    if not isinstance(value, str) or not value or len(value) > MAX_METHOD_NAME_CHARS:
        raise ValueError(f"{field} must be a non-empty bounded string")
    if value != value.strip() or value != value.casefold() or not _NAME_RE.fullmatch(value):
        raise ValueError(f"{field} must be canonical lowercase text")
    return value


def validate_phase_name(value: Any) -> str:
    return _canonical_name(value, field="workflow phase")


def validate_method_spec(spec: dict[str, Any]) -> None:
    if not isinstance(spec, dict):
        raise ValueError("method spec must be an object")
    if len(_canonical_bytes(spec)) > MAX_METHOD_FILE_BYTES:
        raise ValueError(f"method spec exceeds {MAX_METHOD_FILE_BYTES} bytes")

    allowed = {
        "schema_version",
        "name",
        "version",
        "description",
        "required_phases",
        "requirements",
    }
    extra = set(spec) - allowed
    if extra:
        raise ValueError(f"method spec contains unsupported fields: {sorted(extra)!r}")

    if type(spec.get("schema_version")) is not int or spec["schema_version"] != METHOD_SCHEMA_VERSION:
        raise ValueError(f"method schema_version must be {METHOD_SCHEMA_VERSION}")
    _canonical_name(spec.get("name"), field="method name")

    version = spec.get("version")
    if type(version) is not int or version < 1:
        raise ValueError("method version must be a positive integer")

    description = spec.get("description")
    if (
        not isinstance(description, str)
        or not description.strip()
        or len(description) > MAX_METHOD_DESCRIPTION_CHARS
    ):
        raise ValueError("method description must be a non-empty bounded string")

    phases = spec.get("required_phases")
    if not isinstance(phases, list) or not phases:
        raise ValueError("required_phases must be a non-empty list")
    if len(phases) > MAX_METHOD_PHASES:
        raise ValueError(f"required_phases exceeds {MAX_METHOD_PHASES} items")
    seen: set[str] = set()
    for phase in phases:
        normalized = validate_phase_name(phase)
        if normalized in seen:
            raise ValueError(f"duplicate required phase: {normalized!r}")
        seen.add(normalized)

    requirements = spec.get("requirements")
    if not isinstance(requirements, dict):
        raise ValueError("requirements must be an object")
    if len(requirements) > MAX_METHOD_REQUIREMENTS:
        raise ValueError(f"requirements exceeds {MAX_METHOD_REQUIREMENTS} items")
    for key, value in requirements.items():
        requirement = _canonical_name(key, field="requirement name")
        if requirement not in SUPPORTED_METHOD_REQUIREMENTS:
            raise ValueError(f"unsupported method requirement: {requirement!r}")
        if not isinstance(value, bool):
            raise ValueError("requirement values must be booleans")


def method_digest(spec: dict[str, Any]) -> str:
    validate_method_spec(spec)
    return "sha256:" + hashlib.sha256(_canonical_bytes(spec)).hexdigest()


def validate_method_reference(reference: dict[str, Any]) -> None:
    if not isinstance(reference, dict):
        raise ValueError("method reference must be an object")
    if set(reference) != {"name", "version", "digest"}:
        raise ValueError("method reference must contain exactly name, version, and digest")
    _canonical_name(reference.get("name"), field="method reference name")
    version = reference.get("version")
    if type(version) is not int or version < 1:
        raise ValueError("method reference version must be a positive integer")
    digest = reference.get("digest")
    if not isinstance(digest, str) or not _DIGEST_RE.fullmatch(digest):
        raise ValueError("method reference digest must be sha256:<64 lowercase hex chars>")


def method_reference(spec: dict[str, Any]) -> dict[str, Any]:
    validate_method_spec(spec)
    return {
        "name": spec["name"],
        "version": spec["version"],
        "digest": method_digest(spec),
    }


def require_method_match(reference: dict[str, Any], spec: dict[str, Any]) -> None:
    validate_method_reference(reference)
    validate_method_spec(spec)
    if reference["name"] != spec["name"] or reference["version"] != spec["version"]:
        raise ValueError("method reference identity does not match method spec")
    actual = method_digest(spec)
    if reference["digest"] != actual:
        raise ValueError(
            f"method reference digest mismatch: expected {actual}, got {reference['digest']}"
        )


def list_builtin_method_versions(*, directory: Path | None = None) -> list[dict[str, Any]]:
    """Return every immutable built-in method version.

    Historical definitions stay loadable because persisted workflow manifests pin both
    method version and digest. Duplicate name/version identities fail closed.
    """
    root = directory or BUILTIN_METHODS_DIR
    specs: list[dict[str, Any]] = []
    identities: set[tuple[str, int]] = set()
    for path in sorted(root.glob("*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"invalid built-in workflow method: {path}") from exc
        validate_method_spec(payload)
        identity = (str(payload["name"]), int(payload["version"]))
        if identity in identities:
            raise ValueError(f"duplicate built-in workflow method identity: {identity!r}")
        identities.add(identity)
        specs.append(payload)
    return sorted(specs, key=lambda item: (str(item["name"]), int(item["version"])))


def list_builtin_methods(*, directory: Path | None = None) -> list[dict[str, Any]]:
    """Return the latest built-in version for each method name."""
    latest: dict[str, dict[str, Any]] = {}
    for spec in list_builtin_method_versions(directory=directory):
        name = str(spec["name"])
        current = latest.get(name)
        if current is None or int(spec["version"]) > int(current["version"]):
            latest[name] = spec
    return [dict(latest[name]) for name in sorted(latest)]


def load_builtin_method(
    name: str,
    *,
    version: int | None = None,
    directory: Path | None = None,
) -> dict[str, Any]:
    requested_name = _canonical_name(name, field="method name")
    named = [
        spec
        for spec in list_builtin_method_versions(directory=directory)
        if spec["name"] == requested_name
    ]
    if not named:
        suffix = "" if version is None else f" version={version}"
        raise ValueError(f"unknown built-in workflow method: {requested_name!r}{suffix}")
    if version is None:
        return dict(max(named, key=lambda spec: int(spec["version"])))
    matches = [spec for spec in named if spec["version"] == version]
    if not matches:
        raise ValueError(
            f"unknown built-in workflow method: {requested_name!r} version={version}"
        )
    return dict(matches[0])


def _require_planner_checkpoint_after_audit(manifest: dict[str, Any]) -> None:
    audit_ids = {
        str(node["id"])
        for node in manifest.get("nodes", [])
        if node.get("phase") == "audit"
    }
    checkpoints = [
        node
        for node in manifest.get("nodes", [])
        if node.get("kind") == "planner_checkpoint"
    ]
    if not audit_ids or not any(
        audit_ids.issubset(set(node.get("depends_on", [])))
        for node in checkpoints
    ):
        raise ValueError("method requires a planner checkpoint after audit")


def _require_full_verification(manifest: dict[str, Any]) -> None:
    candidates = [
        node
        for node in manifest.get("nodes", [])
        if node.get("kind") == "task" and node.get("phase") == "full_verification"
    ]
    if not any(
        isinstance(node.get("task"), dict)
        and node["task"].get("workflow_policy") == "efficient-verification-v1"
        for node in candidates
    ):
        raise ValueError(
            "method full_verification phase must use efficient-verification-v1"
        )


def _phase_task_nodes(manifest: dict[str, Any], phase: str) -> list[dict[str, Any]]:
    return [
        node
        for node in manifest.get("nodes", [])
        if node.get("kind") == "task" and node.get("phase") == phase
    ]


def _dependency_closure(manifest: dict[str, Any], node_id: str) -> set[str]:
    nodes = {
        str(node["id"]): node
        for node in manifest.get("nodes", [])
        if isinstance(node, dict) and isinstance(node.get("id"), str)
    }
    if node_id not in nodes:
        raise ValueError(f"unknown workflow node while checking method topology: {node_id!r}")
    closure: set[str] = set()
    stack = [str(item) for item in nodes[node_id].get("depends_on", [])]
    while stack:
        dependency = stack.pop()
        if dependency in closure:
            continue
        node = nodes.get(dependency)
        if node is None:
            raise ValueError(
                f"unknown workflow dependency while checking method topology: {dependency!r}"
            )
        closure.add(dependency)
        stack.extend(str(item) for item in node.get("depends_on", []))
    return closure


def _require_multi_repository_implementation(manifest: dict[str, Any]) -> None:
    implementation = _phase_task_nodes(manifest, "implementation")
    repositories = {
        str(node.get("repository_id"))
        for node in implementation
        if isinstance(node.get("repository_id"), str)
    }
    if len(repositories) < 2:
        raise ValueError(
            "method requires implementation tasks across at least two repositories"
        )


def _require_integration_joins_implementation(manifest: dict[str, Any]) -> None:
    implementation_ids = {
        str(node["id"])
        for node in _phase_task_nodes(manifest, "implementation")
    }
    integration = _phase_task_nodes(manifest, "integration")
    if not implementation_ids or not integration:
        raise ValueError(
            "method requires task-based implementation and integration phases"
        )
    if not any(
        implementation_ids.issubset(_dependency_closure(manifest, str(node["id"])))
        for node in integration
    ):
        raise ValueError(
            "method integration phase must depend on all implementation tasks"
        )


def _require_final_review_after_integration(manifest: dict[str, Any]) -> None:
    integration_ids = {
        str(node["id"])
        for node in _phase_task_nodes(manifest, "integration")
    }
    reviews = [
        node
        for node in manifest.get("nodes", [])
        if node.get("phase") == "final_review"
    ]
    if not integration_ids or not reviews:
        raise ValueError("method requires final review after integration")
    if not any(
        integration_ids.issubset(_dependency_closure(manifest, str(node["id"])))
        for node in reviews
    ):
        raise ValueError("method final review must depend on all integration tasks")


def _observed_phases(manifest: dict[str, Any]) -> set[str]:
    observed: set[str] = set()
    for node in manifest.get("nodes", []):
        phase = node.get("phase")
        if phase is not None:
            observed.add(validate_phase_name(phase))
    return observed


def _validate_requirements(
    manifest: dict[str, Any],
    requirements: dict[str, Any],
    *,
    observed: set[str] | None = None,
) -> None:
    adaptive = observed is not None

    if requirements.get("planner_checkpoint_after_audit", False) and (
        not adaptive or "audit" in observed
    ):
        _require_planner_checkpoint_after_audit(manifest)
    if requirements.get("final_full_verification", False) and (
        not adaptive or "full_verification" in observed
    ):
        _require_full_verification(manifest)
    if requirements.get("multi_repository_implementation", False) and (
        not adaptive or "implementation" in observed
    ):
        _require_multi_repository_implementation(manifest)
    if requirements.get("integration_joins_implementation", False) and (
        not adaptive or "integration" in observed
    ):
        _require_integration_joins_implementation(manifest)
    if requirements.get("final_review_after_integration", False) and (
        not adaptive or "final_review" in observed
    ):
        _require_final_review_after_integration(manifest)


def validate_adaptive_workflow_method(
    manifest: dict[str, Any],
    spec: dict[str, Any],
) -> None:
    """Validate an intentionally incomplete method graph between planner checkpoints.

    Identity remains digest-pinned. Structural requirements are enforced as soon as the
    relevant phase appears, while missing future phases are allowed until the effective
    revision lineage is explicitly checked for completion.
    """
    reference = manifest.get("method")
    if not isinstance(reference, dict):
        raise ValueError("workflow method reference is required")
    require_method_match(reference, spec)

    observed = _observed_phases(manifest)
    first_required = str(spec["required_phases"][0])
    if first_required not in observed:
        raise ValueError(
            f"adaptive workflow must contain the first required method phase: {first_required!r}"
        )

    _validate_requirements(manifest, spec["requirements"], observed=observed)


def validate_workflow_method(
    manifest: dict[str, Any],
    spec: dict[str, Any],
) -> None:
    reference = manifest.get("method")
    if not isinstance(reference, dict):
        raise ValueError("workflow method reference is required")
    require_method_match(reference, spec)

    observed_phases = _observed_phases(manifest)
    missing = [
        phase for phase in spec["required_phases"] if phase not in observed_phases
    ]
    if missing:
        raise ValueError(f"workflow is missing required method phases: {missing!r}")

    _validate_requirements(manifest, spec["requirements"])
