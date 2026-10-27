"""Pure host namespace and target-admission contracts for a future GitHub transport.

This module neither enrolls hosts nor consumes tasks. Enrollment records must arrive
from a separately trusted operator-controlled source, not host-authored status files.
The legacy repository queue remains authoritative until an explicit cutover.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path, PurePosixPath
from typing import Any, Mapping

from local_agent.repository.binding import AgentBindingRecord, canonical_agent_binding
from local_agent.repository.context import RepositoryContext
from local_agent.runtime.task_contract import require_task_agent_binding

HOST_ROUTING_VERSION = 1
HOST_NAMESPACE_ROOT = PurePosixPath('.agent/hosts')
_MAX_ENROLLED_REPOSITORIES = 64
_MAX_ENROLLED_HOSTS = 32
_MAX_OPERATOR_REGISTRY_BYTES = 64 * 1024
_MAX_HOST_STATUS_AGE_SECONDS = 3600
_MAX_HOST_STATUS_RECORD_BYTES = 4096
_HOST_STATUS_FIELDS = frozenset({
    "version", "host_id", "installation_id", "source_revision", "updated_at", "state",
})
_MAX_FUTURE_SKEW_SECONDS = 30
_HOST_ID_RE = re.compile(r'^[a-z][a-z0-9-]{2,63}$')
_TASK_ID_RE = re.compile(r'^[A-Za-z0-9._-]{1,200}$')
_REPOSITORY_ID_RE = re.compile(r'^[A-Za-z0-9._-]{1,100}$')
_GIT_SHA_RE = re.compile(r'^[0-9a-f]{40}$')


def canonical_host_id(value: object) -> str:
    """Validate a stable logical host ID; hostname is not host identity."""
    if not isinstance(value, str) or _HOST_ID_RE.fullmatch(value) is None:
        raise ValueError('host_id must be lowercase canonical text (3..64 characters)')
    return value


def _installation_id(value: object) -> str:
    # A canonical UUID is collision-resistant identity, NOT a credential or proof of trust.
    return canonical_agent_binding(value, field='installation_id')


def _task_id(value: object) -> str:
    if not isinstance(value, str) or _TASK_ID_RE.fullmatch(value) is None:
        raise ValueError('task id must be a bounded canonical filename component')
    return value


def host_record_path(host_id: str, record: str) -> PurePosixPath:
    canonical_host_id(host_id)
    if record not in {'identity.json', 'system.json', 'capabilities.json', 'status.json'}:
        raise ValueError('unknown host record name')
    return HOST_NAMESPACE_ROOT / host_id / record


def reject_host_namespace_symlinks(control_root: Path, relative: str | PurePosixPath) -> None:
    """Reject aliases through another host mailbox before reading or writing.

    The regular Git checkout must contain real directories all the way to the
    selected host path. Normal legacy control paths are not affected.
    """
    path = PurePosixPath(relative)
    if path.parts[:2] != (".agent", "hosts"):
        return
    root = Path(control_root)
    current = root
    for component in path.parts[:-1]:
        current = current / component
        if current.is_symlink():
            raise ValueError("host mailbox directory is a symlink")


def host_task_path(host_id: str, task_id: str, *, result: bool = False) -> PurePosixPath:
    canonical_host_id(host_id)
    _task_id(task_id)
    return HOST_NAMESPACE_ROOT / host_id / ('results' if result else 'tasks') / f'{task_id}.json'


@dataclass(frozen=True, slots=True)
class HostIdentity:
    host_id: str
    installation_id: str

    def __post_init__(self) -> None:
        canonical_host_id(self.host_id)
        _installation_id(self.installation_id)

    def as_record(self) -> dict[str, object]:
        return {
            'version': HOST_ROUTING_VERSION,
            'host_id': self.host_id,
            'installation_id': self.installation_id,
        }

    @classmethod
    def from_record(cls, record: Mapping[str, Any]) -> HostIdentity:
        expected_fields = {'version', 'host_id', 'installation_id'}
        if (not isinstance(record, Mapping) or set(record) != expected_fields
                or type(record.get('version')) is not int
                or record['version'] != HOST_ROUTING_VERSION):
            raise ValueError('unsupported or ambiguous local host identity record')
        return cls(host_id=canonical_host_id(record.get('host_id')),
                   installation_id=_installation_id(record.get('installation_id')))


@dataclass(frozen=True, slots=True)
class HostEnrollment:
    """Parsed policy data, not an authorization credential or verified provenance."""

    identity: HostIdentity
    enabled: bool
    repositories: tuple[tuple[str, str], ...]

    @classmethod
    def from_operator_record(cls, record: Mapping[str, Any]) -> HostEnrollment:
        expected_fields = {
            'version', 'host_id', 'installation_id', 'enabled', 'repositories',
        }
        if (not isinstance(record, Mapping) or set(record) != expected_fields
                or type(record.get('version')) is not int
                or record['version'] != HOST_ROUTING_VERSION):
            raise ValueError('unsupported or ambiguous operator enrollment record')
        identity = HostIdentity.from_record({
            key: record[key] for key in ('version', 'host_id', 'installation_id')
        })
        enabled = record.get('enabled')
        if type(enabled) is not bool:
            raise ValueError('enrollment enabled must be boolean')
        targets = record.get('repositories')
        if not isinstance(targets, list) or len(targets) > _MAX_ENROLLED_REPOSITORIES:
            raise ValueError('enrollment repositories must be a bounded list')
        parsed: list[tuple[str, str]] = []
        seen: set[str] = set()
        for target in targets:
            if not isinstance(target, dict) or set(target) != {'repository_id', 'agent_binding'}:
                raise ValueError('enrollment repository must contain only supported fields')
            repo_id = target.get('repository_id')
            if not isinstance(repo_id, str) or _REPOSITORY_ID_RE.fullmatch(repo_id) is None:
                raise ValueError('enrollment repository_id is invalid')
            key = repo_id.casefold()
            if key in seen:
                raise ValueError('duplicate enrollment repository_id')
            seen.add(key)
            parsed.append((repo_id, canonical_agent_binding(target.get('agent_binding'))))
        return cls(identity=identity, enabled=enabled, repositories=tuple(parsed))


def validate_host_enrollments(records: list[Mapping[str, Any]]) -> tuple[HostEnrollment, ...]:
    """Reject duplicate hosts and cross-host writable target collisions pre-fencing.

    Once shared GitHub fencing is implemented, cross-host ownership can evolve, but
    it must never be inferred from local OS locks or self-published host status.
    """
    if not isinstance(records, list) or len(records) > _MAX_ENROLLED_HOSTS:
        raise ValueError('operator host registry must be a bounded list')
    enrollments: list[HostEnrollment] = []
    host_ids: set[str] = set()
    installation_ids: set[str] = set()
    owners_by_binding: dict[str, str] = {}
    owners_by_repository_id: dict[str, str] = {}
    for raw in records:
        enrollment = HostEnrollment.from_operator_record(raw)
        identity = enrollment.identity
        if identity.host_id in host_ids or identity.installation_id in installation_ids:
            raise ValueError('duplicate host id or installation id')
        host_ids.add(identity.host_id)
        installation_ids.add(identity.installation_id)
        if enrollment.enabled:
            for repo_id, binding in enrollment.repositories:
                key = repo_id.casefold()
                if binding in owners_by_binding or key in owners_by_repository_id:
                    raise ValueError('repository assigned to multiple active hosts without distributed fencing')
                owners_by_binding[binding] = identity.host_id
                owners_by_repository_id[key] = identity.host_id
        enrollments.append(enrollment)
    return tuple(enrollments)


def parse_operator_host_registry(raw: bytes) -> tuple[HostEnrollment, ...]:
    """Decode a bounded versioned enrollment manifest from a TRUSTED operator ref.

    The caller must verify the GitHub repository, `operator-control` ref and its
    provenance before passing bytes here. Self-reported host files cannot enroll.
    Missing registry means no host is authorized; there is no legacy fallback.
    """
    if not isinstance(raw, bytes) or not 0 < len(raw) <= _MAX_OPERATOR_REGISTRY_BYTES:
        raise ValueError('operator host registry must be non-empty bounded bytes')

    def strict_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        value: dict[str, Any] = {}
        for key, item in pairs:
            if key in value:
                raise ValueError(f'duplicate operator host registry key: {key}')
            value[key] = item
        return value

    def reject_constant(value: str) -> None:
        raise ValueError(f'non-finite registry number: {value}')

    try:
        data = json.loads(
            raw.decode('utf-8'), object_pairs_hook=strict_pairs,
            parse_constant=reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError('operator host registry must be canonical UTF-8 JSON') from exc
    if (
        not isinstance(data, dict)
        or type(data.get('version')) is not int
        or data['version'] != HOST_ROUTING_VERSION
        or set(data) != {'version', 'hosts'}
    ):
        raise ValueError('unsupported operator host registry envelope')
    return validate_host_enrollments(data.get('hosts'))



def require_host_task_target(
    task: Mapping[str, Any],
    *,
    local_identity: HostIdentity,
    enrollment: HostEnrollment,
    repository: RepositoryContext | AgentBindingRecord,
) -> None:
    """Fail closed for a future host-scoped inbox; never admit legacy task paths here."""
    if not isinstance(task, Mapping):
        raise ValueError('host task must be an object')
    if not enrollment.enabled or enrollment.identity != local_identity:
        raise ValueError('local host is not enrolled or enrollment has been revoked')
    if task.get('target_host_id') != local_identity.host_id:
        raise ValueError('task target_host_id does not match enrolled local host')
    if task.get('target_installation_id') != local_identity.installation_id:
        raise ValueError('task target_installation_id does not match enrolled installation')
    if task.get('target_repository_id') != repository.repository_id:
        raise ValueError('task target_repository_id does not match repository context')
    if repository.agent_binding is None:
        raise ValueError('repository has no canonical agent_binding')
    allowed = {(repo_id.casefold(), binding) for repo_id, binding in enrollment.repositories}
    if (repository.repository_id.casefold(), repository.agent_binding) not in allowed:
        raise ValueError('repository is not authorized by host enrollment')
    require_task_agent_binding(dict(task), repository.agent_binding)
    _task_id(task.get('id'))


def build_host_status(
    identity: HostIdentity,
    *,
    source_revision: str,
    state: str,
    observed_at: datetime,
) -> dict[str, object]:
    """Construct bounded status evidence for a future host-scoped publisher."""
    if not isinstance(identity, HostIdentity):
        raise ValueError('status identity is invalid')
    if not isinstance(source_revision, str) or _GIT_SHA_RE.fullmatch(source_revision) is None:
        raise ValueError('source_revision must be an exact lowercase Git commit SHA')
    if not isinstance(state, str) or state not in {'idle', 'busy', 'disabled', 'error'}:
        raise ValueError('unsupported host state')
    if not isinstance(observed_at, datetime) or observed_at.tzinfo is None or observed_at.utcoffset() is None:
        raise ValueError('status timestamp must be timezone aware')
    return {
        'version': HOST_ROUTING_VERSION,
        'host_id': identity.host_id,
        'installation_id': identity.installation_id,
        'source_revision': source_revision,
        'updated_at': observed_at.isoformat(),
        'state': state,
    }


def parse_host_status_record(raw: bytes) -> dict[str, Any]:
    """Parse an untrusted bounded status observation, never an authorization.

    This does not prove the publisher identity or the origin of the Git object.
    Callers must separately pin and verify the repository and source revision.
    """
    if not isinstance(raw, bytes) or not 0 < len(raw) <= _MAX_HOST_STATUS_RECORD_BYTES:
        raise ValueError('host status must be bounded non-empty bytes')

    def unique_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError('duplicate host status key')
            result[key] = value
        return result

    try:
        value = json.loads(
            raw.decode('utf-8'), object_pairs_hook=unique_pairs,
            parse_constant=lambda _: (_ for _ in ()).throw(ValueError('invalid host status number')),
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError('host status must be UTF-8 JSON') from exc
    if not _valid_host_status_shape(value):
        raise ValueError('host status has unsupported or invalid fields')
    return value


def _valid_host_status_shape(record: object) -> bool:
    if not isinstance(record, Mapping) or set(record) != _HOST_STATUS_FIELDS:
        return False
    if type(record.get('version')) is not int or record['version'] != HOST_ROUTING_VERSION:
        return False
    try:
        HostIdentity.from_record({k: record[k] for k in ('version', 'host_id', 'installation_id')})
    except ValueError:
        return False
    if not isinstance(record.get('source_revision'), str) or not _GIT_SHA_RE.fullmatch(record['source_revision']):
        return False
    if not isinstance(record.get('state'), str) or record['state'] not in {'idle', 'busy', 'disabled', 'error'}:
        return False
    timestamp = record.get('updated_at')
    if not isinstance(timestamp, str) or len(timestamp) > 64:
        return False
    try:
        observed = datetime.fromisoformat(timestamp)
    except ValueError:
        return False
    return observed.tzinfo is not None and observed.utcoffset() is not None


def host_status_observation(
    record: Mapping[str, Any], *, local_identity: HostIdentity,
    now: datetime, max_age_seconds: int = 120,
) -> str:
    """Return `idle`, `busy` or `unknown` for display; never execution authority.

    Offline, revoked, malformed, disabled and expired observations are unknown.
    Source origin and operator enrollment must be checked separately.
    """
    if type(max_age_seconds) is not int or not 1 <= max_age_seconds <= _MAX_HOST_STATUS_AGE_SECONDS:
        raise ValueError('invalid heartbeat freshness interval')
    if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
        raise ValueError('now must be timezone aware')
    if not isinstance(local_identity, HostIdentity) or not _valid_host_status_shape(record):
        return 'unknown'
    if (record['host_id'] != local_identity.host_id
            or record['installation_id'] != local_identity.installation_id
            or record['state'] not in {'idle', 'busy'}):
        return 'unknown'
    updated = datetime.fromisoformat(record['updated_at'])
    age = now - updated
    if not timedelta(seconds=-_MAX_FUTURE_SKEW_SECONDS) <= age <= timedelta(seconds=max_age_seconds):
        return 'unknown'
    return record['state']


def host_status_is_fresh(
    record: Mapping[str, Any], *, local_identity: HostIdentity,
    now: datetime, max_age_seconds: int = 120,
) -> bool:
    """Backwards-compatible freshness hint; cannot grant task execution."""
    return host_status_observation(
        record, local_identity=local_identity, now=now,
        max_age_seconds=max_age_seconds,
    ) in {'idle', 'busy'}
