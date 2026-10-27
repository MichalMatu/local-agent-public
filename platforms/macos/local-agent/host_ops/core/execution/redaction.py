"""Small explicit redaction primitives for evidence surfaces."""

from __future__ import annotations

from collections.abc import Iterable

REDACTION_MARKER = "***REDACTED***"


def redact_text(
    text: str,
    sensitive_values: Iterable[str],
    *,
    marker: str = REDACTION_MARKER,
) -> str:
    """Replace explicitly supplied sensitive values without guessing at secrets."""

    values = sorted({value for value in sensitive_values if value}, key=len, reverse=True)
    redacted = text
    for value in values:
        redacted = redacted.replace(value, marker)
    return redacted
