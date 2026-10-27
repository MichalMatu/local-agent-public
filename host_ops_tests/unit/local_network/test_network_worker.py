from __future__ import annotations

import json
import socket

from local_agent.host_ops.capabilities.local.network import _worker


class _FakeConnection:
    family = socket.AF_INET

    def __enter__(self) -> _FakeConnection:
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        return None

    def getpeername(self) -> tuple[str, int]:
        return ("127.0.0.1", 1234)


def test_worker_rejects_missing_operation(capsys) -> None:
    assert _worker.main(()) == 2
    assert json.loads(capsys.readouterr().out)["error"] == "missing network worker operation"


def test_worker_rejects_invalid_operation_shape(capsys) -> None:
    assert _worker.main(("resolve", "localhost", "extra")) == 2
    assert json.loads(capsys.readouterr().out)["error"] == "invalid network worker arguments"


def test_worker_rejects_invalid_tcp_numbers(capsys) -> None:
    assert _worker.main(("tcp", "localhost", "bad-port", "1")) == 2
    assert json.loads(capsys.readouterr().out)["error"] == "invalid tcp worker arguments"


def test_worker_resolve_emits_sorted_supported_addresses(monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        _worker.socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: [
            (socket.AF_INET6, socket.SOCK_STREAM, 0, "", ("::1", 0, 0, 0)),
            (socket.AF_UNIX, socket.SOCK_STREAM, 0, "", ("ignored",)),
            (socket.AF_INET, socket.SOCK_STREAM, 0, "", ("127.0.0.1", 0)),
            (socket.AF_INET, socket.SOCK_STREAM, 0, "", ("127.0.0.1", 0)),
        ],
    )

    assert _worker.main(("resolve", "localhost")) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload == {
        "addresses": [
            {"address": "127.0.0.1", "family": "ipv4"},
            {"address": "::1", "family": "ipv6"},
        ],
        "error": None,
    }


def test_worker_resolve_reports_socket_error(monkeypatch, capsys) -> None:
    def fail(*_args, **_kwargs):
        raise socket.gaierror("not found")

    monkeypatch.setattr(_worker.socket, "getaddrinfo", fail)

    assert _worker.main(("resolve", "missing.invalid")) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["addresses"] == []
    assert "gaierror" in payload["error"]


def test_worker_tcp_reports_success(monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        _worker.socket,
        "create_connection",
        lambda *_args, **_kwargs: _FakeConnection(),
    )

    assert _worker.main(("tcp", "127.0.0.1", "1234", "2.5")) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload == {
        "connected": True,
        "error": None,
        "family": "ipv4",
        "peer_address": "127.0.0.1",
    }


def test_worker_tcp_reports_connection_error(monkeypatch, capsys) -> None:
    def fail(*_args, **_kwargs):
        raise ConnectionRefusedError("refused")

    monkeypatch.setattr(_worker.socket, "create_connection", fail)

    assert _worker.main(("tcp", "127.0.0.1", "1234", "2.5")) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["connected"] is False
    assert payload["peer_address"] is None
    assert payload["family"] is None
    assert "ConnectionRefusedError" in payload["error"]
