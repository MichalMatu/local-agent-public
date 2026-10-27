from __future__ import annotations

CHILD_STATES = frozenset(
    {
        "requested",
        "registration_pending",
        "active",
        "terminal_pending_evidence",
        "terminal_recorded",
        "retired",
        "cancelled",
        "abandoned",
    }
)

_ALLOWED_TRANSITIONS: dict[str, frozenset[str]] = {
    "requested": frozenset({"registration_pending", "cancelled", "abandoned"}),
    "registration_pending": frozenset({"active", "cancelled", "abandoned"}),
    "active": frozenset({"terminal_pending_evidence", "cancelled", "abandoned"}),
    "terminal_pending_evidence": frozenset({"terminal_recorded"}),
    "terminal_recorded": frozenset({"retired"}),
    "retired": frozenset(),
    "cancelled": frozenset(),
    "abandoned": frozenset(),
}


def validate_child_state(value: str) -> str:
    if not isinstance(value, str) or value not in CHILD_STATES:
        raise ValueError(f"unsupported child lifecycle state: {value!r}")
    return value


def initial_child_state() -> str:
    return "requested"


def transition_child_state(current: str, target: str) -> str:
    current = validate_child_state(current)
    target = validate_child_state(target)
    if current == target:
        return current
    if target not in _ALLOWED_TRANSITIONS[current]:
        raise ValueError(f"invalid child lifecycle transition: {current} -> {target}")
    return target
