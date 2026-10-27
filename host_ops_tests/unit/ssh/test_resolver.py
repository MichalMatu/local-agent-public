from __future__ import annotations

import pytest

from local_agent.host_ops.capabilities.remote.ssh import UnknownHostError, resolve_host
from local_agent.host_ops.core.config import HostOpsConfig, HostTarget


def test_resolve_host_returns_configured_target() -> None:
    target = HostTarget(alias="phone", host="192.0.2.100", port=8022, user="u0_a123")
    config = HostOpsConfig(hosts={"phone": target})

    assert resolve_host(config, "phone") is target


def test_resolve_host_fails_closed_for_unknown_alias() -> None:
    with pytest.raises(UnknownHostError, match="unknown host alias"):
        resolve_host(HostOpsConfig(), "missing")


@pytest.mark.parametrize("host", ["-oProxyCommand=bad", "user@example.test", "bad host"])
def test_host_target_rejects_unsafe_destination(host: str) -> None:
    with pytest.raises(ValueError):
        HostTarget(alias="phone", host=host)


@pytest.mark.parametrize("user", ["-root", "user@other", "bad user"])
def test_host_target_rejects_unsafe_user(user: str) -> None:
    with pytest.raises(ValueError):
        HostTarget(alias="phone", host="example.test", user=user)
