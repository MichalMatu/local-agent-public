"""Owner-selected project enrollment in the private installation catalog.

The caller holds the exclusive offline installation lock while creating a
binding and registering the selected repository. This module never changes
the bundled catalog, an existing control checkout, or a running daemon.
"""
from __future__ import annotations

import json
import secrets
import uuid
from pathlib import Path

from local_agent.repository.binding import (
    CATALOG_VERSION, AgentBindingRecord, load_binding_catalog,
    local_binding_catalog_path,
)
from local_agent.repository.context import repository_from_dict
from local_agent.repository.transactional_add import _atomic_json


def ensure_local_repository_binding(repository_id: str, repository: str) -> str:
    """Create/reuse an identity only for an explicit offline owner selection.

    Call only while holding _require_offline_installation across the subsequent
    registration. A catalog entry alone does not register a project or authorize
    a project task. Failed registration leaves a retryable, unregistered identity.
    """
    repository_from_dict(
        {"id": repository_id, "repository": repository},
        home=Path.home(),
    )
    catalog = load_binding_catalog()
    matched: AgentBindingRecord | None = None
    for record in catalog:
        if (record.repository_id.casefold() == repository_id.casefold()
                or record.repository.casefold() == repository.casefold()):
            if (record.repository_id.casefold() != repository_id.casefold()
                    or record.repository.casefold() != repository.casefold()):
                raise ValueError("binding identity conflicts with an existing project")
            matched = record
    if matched is not None:
        if not matched.execution_enabled:
            raise ValueError("existing canonical project binding is disabled")
        return matched.agent_binding

    path = local_binding_catalog_path()
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise ValueError("private binding catalog is not a regular file")
    if path.exists():
        if path.stat().st_size > 1_000_000:
            raise ValueError("private binding catalog is oversized")
        payload = json.loads(path.read_text(encoding="utf-8"))
    else:
        payload = {"version": CATALOG_VERSION, "agents": []}
    if (not isinstance(payload, dict)
            or type(payload.get("version")) is not int
            or payload["version"] != CATALOG_VERSION
            or not isinstance(payload.get("agents"), list)):
        raise ValueError("private binding catalog has an invalid version or structure")
    used = {record.agent_binding for record in catalog}
    binding = str(uuid.UUID(bytes=secrets.token_bytes(16), version=4))
    while binding in used:
        binding = str(uuid.UUID(bytes=secrets.token_bytes(16), version=4))
    payload["agents"].append({
        "id": repository_id,
        "repository": repository,
        "agent_binding": binding,
        "execution_enabled": True,
        "planner_scope": "repository",
    })
    _atomic_json(path, payload)
    # Re-read the full policy, including source, and fail if any identity conflicts.
    confirmed = load_binding_catalog()
    if not any(record.repository_id == repository_id
               and record.repository.casefold() == repository.casefold()
               and record.agent_binding == binding for record in confirmed):
        raise RuntimeError("new canonical binding could not be verified")
    return binding
