"""Immutable operator approvals for repository-independent remote host tasks.

An agent-control branch writer cannot make a host execute arbitrary commands:
the exact task digest must also appear in the independently pinned operator-
control branch. Parsing policy does NOT authorize a Git writer or execute work.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Mapping

from local_agent.repository.host_routing import HostIdentity

HOST_COMMAND_GRANT_VERSION = 1
MAX_GRANTS = 256
MAX_GRANT_BYTES = 64 * 1024
_ID = re.compile(r"^[A-Za-z0-9._-]{1,120}$")
_DIGEST = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True, slots=True)
class HostCommandGrant:
    identity: HostIdentity
    task_id: str
    digest: str

    @classmethod
    def from_record(cls, record: Mapping[str, Any]) -> "HostCommandGrant":
        if not isinstance(record, dict) or set(record) != {
            "host_id", "installation_id", "task_id", "task_digest",
        }:
            raise ValueError("host command grant has unsupported fields")
        identity = HostIdentity(
            host_id=record["host_id"],
            installation_id=record["installation_id"],
        )
        task_id = record["task_id"]
        digest = record["task_digest"]
        if not isinstance(task_id, str) or _ID.fullmatch(task_id) is None:
            raise ValueError("host command grant task id is invalid")
        if not isinstance(digest, str) or _DIGEST.fullmatch(digest) is None:
            raise ValueError("host command grant task digest is invalid")
        return cls(identity=identity, task_id=task_id, digest=digest)


def parse_host_command_grants(raw: bytes) -> tuple[HostCommandGrant, ...]:
    """Parse a bounded manifest; caller MUST prove operator-control provenance.

    A parsed grant alone isn't authority: require the live pinned operator SHA,
    enabled host enrollment, exact Git snapshot and request digest at intake.
    """
    if not isinstance(raw, bytes) or not 0 < len(raw) <= MAX_GRANT_BYTES:
        raise ValueError("host command approval manifest exceeds its byte bound")

    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate operator host command grant key")
            result[key] = value
        return result

    try:
        payload = json.loads(
            raw.decode("utf-8"), object_pairs_hook=pairs,
            parse_constant=lambda x: (_ for _ in ()).throw(
                ValueError(f"invalid nonfinite manifest constant {x}"),
            ),
        )
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("host command approval manifest is not canonical JSON") from exc
    if (
        not isinstance(payload, dict)
        or set(payload) != {"version", "grants"}
        or type(payload.get("version")) is not int
        or payload["version"] != HOST_COMMAND_GRANT_VERSION
        or not isinstance(payload.get("grants"), list)
        or len(payload["grants"]) > MAX_GRANTS
    ):
        raise ValueError("host command approval manifest has invalid schema")
    seen: set[tuple[str, str, str]] = set()
    records: list[HostCommandGrant] = []
    for raw_record in payload["grants"]:
        record = HostCommandGrant.from_record(raw_record)
        key = (record.identity.host_id, record.identity.installation_id, record.task_id)
        if key in seen:
            raise ValueError("duplicate operator host command grant")
        seen.add(key)
        records.append(record)
    return tuple(records)


def require_exact_host_command_grant(
    *,
    identity: HostIdentity,
    task_id: str,
    task_digest: str,
    grants: tuple[HostCommandGrant, ...],
) -> None:
    """Fail closed on absent, revoked, changed, stale or mistargeted commands."""
    if not isinstance(identity, HostIdentity) or not isinstance(grants, tuple):
        raise ValueError("host task operator approval is not verified")
    if not isinstance(task_id, str) or _ID.fullmatch(task_id) is None:
        raise ValueError("host command task id is invalid")
    if not isinstance(task_digest, str) or _DIGEST.fullmatch(task_digest) is None:
        raise ValueError("host command digest is invalid")
    if not any(
        isinstance(record, HostCommandGrant)
        and record.identity == identity
        and record.task_id == task_id
        and record.digest == task_digest
        for record in grants
    ):
        raise ValueError("operator has not approved this exact host command")
