from __future__ import annotations

import pytest

from local_agent.host_ops.capabilities.local.browser import BrowserInspector
from local_agent.host_ops.capabilities.local.browser._validation import validate_timeout_seconds


class FailingRunner:
    def run(self, _argv, *, limits=None):
        raise AssertionError("invalid timeout must fail before process inspection")


@pytest.mark.parametrize(
    "value",
    [True, False, float("nan"), float("inf"), -float("inf"), "1", None],
)
def test_timeout_validation_rejects_boolean_nonfinite_and_nonnumeric_values(value) -> None:
    with pytest.raises(ValueError, match="timeout_seconds"):
        validate_timeout_seconds(value)


@pytest.mark.parametrize(
    ("value", "minimum"),
    [(0.1, 0.1), (1, 1.0), (3.0, 3.0), (120, 1.0)],
)
def test_timeout_validation_accepts_and_normalizes_bounds(value: float, minimum: float) -> None:
    assert validate_timeout_seconds(value, minimum=minimum) == float(value)


@pytest.mark.parametrize("value", [True, float("nan"), float("inf"), 0, 120.1])
def test_browser_inspection_rejects_invalid_timeout_before_process_effect(value) -> None:
    with pytest.raises(ValueError, match="timeout_seconds"):
        BrowserInspector(FailingRunner()).inspect(timeout_seconds=value)
