from __future__ import annotations

import math

import pytest

from local_agent.host_ops.core.execution import ExecutionLimits, redact_text


def test_limits_reject_non_positive_values() -> None:
    with pytest.raises(ValueError):
        ExecutionLimits(timeout_seconds=0)
    with pytest.raises(ValueError):
        ExecutionLimits(max_stdout_bytes=0)


@pytest.mark.parametrize("value", [True, False, math.inf, -math.inf, math.nan, "30"])
def test_timeout_rejects_ambiguous_or_non_finite_values(value) -> None:
    with pytest.raises(ValueError):
        ExecutionLimits(timeout_seconds=value)


def test_redact_text_replaces_longest_values_first() -> None:
    assert (
        redact_text(
            "token=secret-long secret",
            ["secret", "secret-long", ""],
        )
        == "token=***REDACTED*** ***REDACTED***"
    )
