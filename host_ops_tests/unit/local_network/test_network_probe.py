from __future__ import annotations

import socket

import pytest

from local_agent.host_ops.capabilities.local.network import NetworkProbeError, NetworkProber


def test_resolve_localhost_returns_ip_addresses() -> None:
    result = NetworkProber().resolve("localhost", timeout_seconds=2.0)

    assert result.ok
    assert result.error is None
    assert result.addresses
    assert all(item.family in {"ipv4", "ipv6"} for item in result.addresses)
    assert result.as_dict()["host"] == "localhost"


def test_tcp_connects_to_loopback_listener() -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        port = listener.getsockname()[1]

        result = NetworkProber().tcp("127.0.0.1", port, timeout_seconds=2.0)

    assert result.ok
    assert result.connected is True
    assert result.peer_address == "127.0.0.1"
    assert result.family == "ipv4"


def test_tcp_reports_refused_connection() -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as reserved:
        reserved.bind(("127.0.0.1", 0))
        port = reserved.getsockname()[1]

        result = NetworkProber().tcp("127.0.0.1", port, timeout_seconds=2.0)

    assert result.ok is False
    assert result.connected is False
    assert result.error


@pytest.mark.parametrize("host", ["", " ", "https://example.com", "a b", "bad/path"])
def test_probe_rejects_invalid_hosts(host: str) -> None:
    with pytest.raises(NetworkProbeError):
        NetworkProber().resolve(host)


@pytest.mark.parametrize("port", [0, 65536, -1, True])
def test_probe_rejects_invalid_ports(port: int) -> None:
    with pytest.raises(NetworkProbeError):
        NetworkProber().tcp("localhost", port)


@pytest.mark.parametrize("timeout", [0.0, -1.0, 61.0, float("inf")])
def test_probe_rejects_invalid_timeouts(timeout: float) -> None:
    with pytest.raises(NetworkProbeError):
        NetworkProber().resolve("localhost", timeout_seconds=timeout)
