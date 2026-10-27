"""Versioned Git-history policy markers for Local Agent control branches."""
from __future__ import annotations

CONTROL_HISTORY_POLICY_TRAILER = "Local-Agent-Control-History: bounded-v1"
CONTROL_HISTORY_COMPACT_ROOT_MESSAGE = (
    "Compact local-agent control history\n\n"
    f"{CONTROL_HISTORY_POLICY_TRAILER}"
)
CONTROL_HISTORY_INITIAL_ROOT_MESSAGE = (
    "Initialize local-agent control branch\n\n"
    f"{CONTROL_HISTORY_POLICY_TRAILER}"
)
