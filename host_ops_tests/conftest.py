from __future__ import annotations

import pytest

_LOCAL_AGENT_INTERNAL_ENV_NAMES = (
    "LOCAL_AGENT_LEASE_FDS",
    "LOCAL_AGENT_RESOURCE_LEASE_FDS",
    "LOCAL_AGENT_LEASE_KEYS_DIGEST",
)


@pytest.fixture(autouse=True)
def isolate_local_agent_lease_context(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep tests deterministic when pytest itself runs beneath Local Agent."""

    for name in _LOCAL_AGENT_INTERNAL_ENV_NAMES:
        monkeypatch.delenv(name, raising=False)
