from __future__ import annotations

import math


def validate_timeout_seconds(
    value: float,
    *,
    minimum: float = 1.0,
    maximum: float = 120.0,
) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, int | float)
        or not math.isfinite(value)
        or not minimum <= value <= maximum
    ):
        raise ValueError(f"timeout_seconds must be at least {minimum:g} and at most {maximum:g}")
    return float(value)
