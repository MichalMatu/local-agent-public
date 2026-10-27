"""Versioned internal Local Agent <-> Tool Runtime data contract.

This module owns representation, validation and deterministic bounded serialization only.
It does not discover tools, admit tasks, derive scheduler resources or execute effects.
ExecutionLimits.timeout_seconds represents the whole-operation budget supplied by the caller;
composite capabilities remain responsible for deriving and enforcing monotonic deadlines.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Mapping

from local_agent.host_ops.core.execution import ExecutionLimits, ProcessResult

TOOL_RUNTIME_SCHEMA_VERSION = 1
MAX_TOOL_ID_CHARS = 160
MAX_TARGET_NAME_CHARS = 1024
MAX_METADATA_BYTES = 256 * 1024
MAX_ARGUMENT_BYTES = 512 * 1024
MAX_RESULT_PAYLOAD_BYTES = 2 * 1024 * 1024
MAX_CONTRACT_BYTES = 4 * 1024 * 1024
MAX_ARTIFACTS = 32
MAX_SCHEDULER_RESOURCES = 8
MAX_ERROR_MESSAGE_CHARS = 1_048_576

_TOOL_ID_RE = re.compile(r"^[a-z][a-z0-9._-]*$")
_NAME_RE = re.compile(r"^[a-z][a-z0-9._-]*$")
_RESOURCE_RE = re.compile(r"^[a-z0-9._:-]+$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class SemanticEffect(StrEnum):
    PASSIVE_READ = "PASSIVE_READ"
    ACTIVE_READ = "ACTIVE_READ"
    MUTATION = "MUTATION"
    DISRUPTIVE = "DISRUPTIVE"


class AuthorityCeiling(StrEnum):
    NONE = "NONE"
    FIXED_LOCAL_EXEC = "FIXED_LOCAL_EXEC"
    FIXED_REMOTE_DEVICE_EXEC = "FIXED_REMOTE_DEVICE_EXEC"
    FIXED_DEVICE_IO = "FIXED_DEVICE_IO"
    ARBITRARY_CODE_LIKE = "ARBITRARY_CODE_LIKE"


def validate_schema_version(value: object) -> int:
    if type(value) is not int or value != TOOL_RUNTIME_SCHEMA_VERSION:
        raise ValueError(
            f"tool runtime schema_version must be {TOOL_RUNTIME_SCHEMA_VERSION}"
        )
    return value


def _validate_name(value: object, *, field: str, maximum: int) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > maximum
        or not _NAME_RE.fullmatch(value)
    ):
        raise ValueError(f"{field} must be a bounded canonical identifier")
    return value


def _validate_tool_id(value: object) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > MAX_TOOL_ID_CHARS
        or not _TOOL_ID_RE.fullmatch(value)
    ):
        raise ValueError("tool_id must be a bounded canonical identifier")
    return value


def _validate_text(value: object, *, field: str, maximum: int) -> str:
    if not isinstance(value, str) or len(value) > maximum:
        raise ValueError(f"{field} must be text up to {maximum} characters")
    return value


def _validate_json_value(value: object, *, field: str, depth: int = 0) -> None:
    if depth > 24:
        raise ValueError(f"{field} exceeds maximum JSON nesting")
    if value is None or isinstance(value, str | bool | int):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"{field} must not contain NaN or infinity")
        return
    if isinstance(value, list):
        for index, item in enumerate(value):
            _validate_json_value(item, field=f"{field}[{index}]", depth=depth + 1)
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError(f"{field} object keys must be strings")
            _validate_json_value(item, field=f"{field}.{key}", depth=depth + 1)
        return
    raise ValueError(f"{field} must contain canonical JSON data")


def _canonical_bytes(payload: object) -> bytes:
    try:
        encoded = json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ValueError("tool runtime payload must be canonical JSON data") from exc
    return encoded


def _freeze_json(value: object) -> object:
    """Detach validated JSON records from callers and make nested data read-only."""
    if isinstance(value, dict):
        return MappingProxyType({key: _freeze_json(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze_json(item) for item in value)
    return value


def _plain_json(value: object) -> object:
    """Return a fresh ordinary JSON structure for external serialization."""
    if isinstance(value, Mapping):
        return {key: _plain_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_plain_json(item) for item in value]
    return value


def _validated_mapping(
    value: object,
    *,
    field: str,
    max_bytes: int,
) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{field} must be an object")
    result = dict(value)
    _validate_json_value(result, field=field)
    if len(_canonical_bytes(result)) > max_bytes:
        raise ValueError(f"{field} exceeds {max_bytes} bytes")
    return _freeze_json(result)


def _limits_dict(limits: ExecutionLimits) -> dict[str, object]:
    return {
        "timeout_seconds": limits.timeout_seconds,
        "terminate_grace_seconds": limits.terminate_grace_seconds,
        "pipe_drain_seconds": limits.pipe_drain_seconds,
        "max_stdout_bytes": limits.max_stdout_bytes,
        "max_stderr_bytes": limits.max_stderr_bytes,
    }


@dataclass(frozen=True, slots=True)
class ToolDescriptor:
    tool_id: str
    effect: SemanticEffect
    authority: AuthorityCeiling

    def __post_init__(self) -> None:
        _validate_tool_id(self.tool_id)
        if not isinstance(self.effect, SemanticEffect):
            raise ValueError("effect must be a SemanticEffect")
        if not isinstance(self.authority, AuthorityCeiling):
            raise ValueError("authority must be an AuthorityCeiling")

    def as_dict(self) -> dict[str, object]:
        return {
            "tool_id": self.tool_id,
            "effect": self.effect.value,
            "authority": self.authority.value,
        }


@dataclass(frozen=True, slots=True)
class TransportLocator:
    kind: str
    attributes: Mapping[str, object]

    def __post_init__(self) -> None:
        _validate_name(self.kind, field="transport locator kind", maximum=80)
        object.__setattr__(
            self,
            "attributes",
            _validated_mapping(
                self.attributes,
                field="transport locator attributes",
                max_bytes=MAX_METADATA_BYTES,
            ),
        )

    def as_dict(self) -> dict[str, object]:
        return {"kind": self.kind, "attributes": _plain_json(self.attributes)}


@dataclass(frozen=True, slots=True)
class IdentityEvidence:
    kind: str
    attributes: Mapping[str, object]

    def __post_init__(self) -> None:
        _validate_name(self.kind, field="identity evidence kind", maximum=80)
        object.__setattr__(
            self,
            "attributes",
            _validated_mapping(
                self.attributes,
                field="identity evidence attributes",
                max_bytes=MAX_METADATA_BYTES,
            ),
        )

    def as_dict(self) -> dict[str, object]:
        return {"kind": self.kind, "attributes": _plain_json(self.attributes)}


@dataclass(frozen=True, slots=True)
class OperationTarget:
    kind: str
    name: str
    locator: TransportLocator | None = None
    identity_evidence: tuple[IdentityEvidence, ...] = ()

    def __post_init__(self) -> None:
        _validate_name(self.kind, field="operation target kind", maximum=80)
        _validate_text(
            self.name,
            field="operation target name",
            maximum=MAX_TARGET_NAME_CHARS,
        )
        if not self.name:
            raise ValueError("operation target name must be non-empty")
        if self.locator is not None and not isinstance(self.locator, TransportLocator):
            raise ValueError("operation target locator must be a TransportLocator")
        if not isinstance(self.identity_evidence, tuple):
            raise ValueError("identity_evidence must be a tuple")
        for item in self.identity_evidence:
            if not isinstance(item, IdentityEvidence):
                raise ValueError("identity_evidence items must be IdentityEvidence")

    def as_dict(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "name": self.name,
            "locator": None if self.locator is None else self.locator.as_dict(),
            "identity_evidence": [item.as_dict() for item in self.identity_evidence],
        }


@dataclass(frozen=True, slots=True)
class ArtifactEvidence:
    path: str
    size_bytes: int
    sha256: str | None = None
    mime_type: str | None = None

    def __post_init__(self) -> None:
        _validate_text(self.path, field="artifact path", maximum=4096)
        if not self.path:
            raise ValueError("artifact path must be non-empty")
        if (
            isinstance(self.size_bytes, bool)
            or not isinstance(self.size_bytes, int)
            or self.size_bytes < 0
        ):
            raise ValueError("artifact size_bytes must be a non-negative integer")
        if self.sha256 is not None and (
            not isinstance(self.sha256, str) or not _SHA256_RE.fullmatch(self.sha256)
        ):
            raise ValueError("artifact sha256 must be a lowercase SHA-256 hex digest")
        if self.mime_type is not None:
            _validate_text(self.mime_type, field="artifact mime_type", maximum=256)

    def as_dict(self) -> dict[str, object]:
        return {
            "path": self.path,
            "size_bytes": self.size_bytes,
            "sha256": self.sha256,
            "mime_type": self.mime_type,
        }


@dataclass(frozen=True, slots=True)
class PartialEffectEvidence:
    action_attempted: bool | None = None
    committed: bool | None = None
    cleanup_failed: bool | None = None
    stage: str | None = None

    def __post_init__(self) -> None:
        for field_name in ("action_attempted", "committed", "cleanup_failed"):
            value = getattr(self, field_name)
            if value is not None and not isinstance(value, bool):
                raise ValueError(f"{field_name} must be boolean or null")
        if self.committed is True and self.action_attempted is False:
            raise ValueError("committed=true requires action_attempted=true")
        if self.stage is not None:
            _validate_text(self.stage, field="partial effect stage", maximum=256)

    def as_dict(self) -> dict[str, object]:
        return {
            "action_attempted": self.action_attempted,
            "committed": self.committed,
            "cleanup_failed": self.cleanup_failed,
            "stage": self.stage,
        }


@dataclass(frozen=True, slots=True)
class ToolError:
    code: str
    message: str

    def __post_init__(self) -> None:
        _validate_name(self.code, field="tool error code", maximum=120)
        _validate_text(
            self.message,
            field="tool error message",
            maximum=MAX_ERROR_MESSAGE_CHARS,
        )

    def as_dict(self) -> dict[str, object]:
        return {"code": self.code, "message": self.message}


def _validate_scheduler_resources(resources: tuple[str, ...]) -> None:
    if not isinstance(resources, tuple):
        raise ValueError("scheduler_resources must be a tuple")
    if len(resources) > MAX_SCHEDULER_RESOURCES:
        raise ValueError(
            f"scheduler_resources exceeds {MAX_SCHEDULER_RESOURCES} items"
        )
    seen: set[str] = set()
    for resource in resources:
        if (
            not isinstance(resource, str)
            or not resource
            or resource != resource.strip()
            or resource != resource.casefold()
            or not _RESOURCE_RE.fullmatch(resource)
        ):
            raise ValueError("scheduler_resources must contain canonical resource names")
        if resource in seen:
            raise ValueError(f"duplicate scheduler resource: {resource!r}")
        seen.add(resource)
    if "machine" in seen and len(resources) != 1:
        raise ValueError("scheduler resource 'machine' must be declared alone")


@dataclass(frozen=True, slots=True)
class ToolInvocation:
    tool: ToolDescriptor
    arguments: Mapping[str, object]
    scheduler_resources: tuple[str, ...]
    execution_limits: ExecutionLimits
    target: OperationTarget | None = None
    schema_version: int = TOOL_RUNTIME_SCHEMA_VERSION

    def __post_init__(self) -> None:
        validate_schema_version(self.schema_version)
        if not isinstance(self.tool, ToolDescriptor):
            raise ValueError("tool must be a ToolDescriptor")
        object.__setattr__(
            self,
            "arguments",
            _validated_mapping(
                self.arguments,
                field="tool arguments",
                max_bytes=MAX_ARGUMENT_BYTES,
            ),
        )
        _validate_scheduler_resources(self.scheduler_resources)
        if not isinstance(self.execution_limits, ExecutionLimits):
            raise ValueError("execution_limits must be ExecutionLimits")
        if self.target is not None and not isinstance(self.target, OperationTarget):
            raise ValueError("target must be an OperationTarget")

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "record_type": "invocation",
            "tool": self.tool.as_dict(),
            "arguments": _plain_json(self.arguments),
            "target": None if self.target is None else self.target.as_dict(),
            "scheduler_resources": list(self.scheduler_resources),
            "execution_limits": _limits_dict(self.execution_limits),
        }


@dataclass(frozen=True, slots=True)
class ToolResult:
    tool: ToolDescriptor
    ok: bool
    payload: Mapping[str, object]
    target: OperationTarget | None = None
    process: ProcessResult | None = None
    artifacts: tuple[ArtifactEvidence, ...] = ()
    partial_effect: PartialEffectEvidence | None = None
    error: ToolError | None = None
    schema_version: int = TOOL_RUNTIME_SCHEMA_VERSION

    def __post_init__(self) -> None:
        validate_schema_version(self.schema_version)
        if not isinstance(self.tool, ToolDescriptor):
            raise ValueError("tool must be a ToolDescriptor")
        if not isinstance(self.ok, bool):
            raise ValueError("ok must be a boolean")
        object.__setattr__(
            self,
            "payload",
            _validated_mapping(
                self.payload,
                field="tool result payload",
                max_bytes=MAX_RESULT_PAYLOAD_BYTES,
            ),
        )
        if self.target is not None and not isinstance(self.target, OperationTarget):
            raise ValueError("target must be an OperationTarget")
        if self.process is not None and not isinstance(self.process, ProcessResult):
            raise ValueError("process must be a ProcessResult")
        if not isinstance(self.artifacts, tuple):
            raise ValueError("artifacts must be a tuple")
        if len(self.artifacts) > MAX_ARTIFACTS:
            raise ValueError(f"artifacts exceeds {MAX_ARTIFACTS} items")
        for artifact in self.artifacts:
            if not isinstance(artifact, ArtifactEvidence):
                raise ValueError("artifacts items must be ArtifactEvidence")
        if self.partial_effect is not None and not isinstance(
            self.partial_effect, PartialEffectEvidence
        ):
            raise ValueError("partial_effect must be PartialEffectEvidence")
        if self.error is not None and not isinstance(self.error, ToolError):
            raise ValueError("error must be a ToolError")
        if self.ok and self.error is not None:
            raise ValueError("successful tool result must not contain an error")

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "record_type": "result",
            "tool": self.tool.as_dict(),
            "ok": self.ok,
            "target": None if self.target is None else self.target.as_dict(),
            "payload": _plain_json(self.payload),
            "process": None if self.process is None else self.process.as_dict(),
            "artifacts": [item.as_dict() for item in self.artifacts],
            "partial_effect": (
                None if self.partial_effect is None else self.partial_effect.as_dict()
            ),
            "error": None if self.error is None else self.error.as_dict(),
        }


def canonical_contract_bytes(value: ToolInvocation | ToolResult) -> bytes:
    if not isinstance(value, ToolInvocation | ToolResult):
        raise TypeError("value must be ToolInvocation or ToolResult")
    encoded = _canonical_bytes(value.as_dict())
    if len(encoded) > MAX_CONTRACT_BYTES:
        raise ValueError(f"tool runtime record exceeds {MAX_CONTRACT_BYTES} bytes")
    return encoded
