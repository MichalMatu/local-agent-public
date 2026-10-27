"""UI-only desktop preferences; never daemon, grant or admission configuration."""
from __future__ import annotations

DEFAULT_REFRESH_SECONDS = 30
MIN_REFRESH_SECONDS = 5
MAX_REFRESH_SECONDS = 600


def safe_refresh_seconds(value: object, *, fallback: int = DEFAULT_REFRESH_SECONDS) -> int:
    """Accept bounded integer values from QSettings without coercing booleans."""
    if type(value) is int:
        seconds = value
    elif isinstance(value, str) and value.isascii() and value.isdecimal():
        seconds = int(value[:8]) if len(value) <= 8 else -1
    else:
        seconds = -1
    if MIN_REFRESH_SECONDS <= seconds <= MAX_REFRESH_SECONDS:
        return seconds
    return fallback
