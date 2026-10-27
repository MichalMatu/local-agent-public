"""Host tasks do not need a project binding; preserve Mac UUID validation for parity."""
from __future__ import annotations
import uuid
def canonical_agent_binding(raw: object, *, field: str = "agent_binding") -> str:
    if not isinstance(raw, str):
        raise ValueError(f"{field} must be a canonical UUID")
    try:
        canonical = str(uuid.UUID(raw))
    except (ValueError, AttributeError):
        raise ValueError(f"{field} must be a canonical UUID") from None
    if raw != canonical:
        raise ValueError(f"{field} must be a canonical UUID")
    return canonical
