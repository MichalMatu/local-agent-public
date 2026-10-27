from __future__ import annotations

import importlib
import json
import socket

cli_main = importlib.import_module("local_agent.host_ops.cli.main")


def test_network_parser_exposes_resolve_contract() -> None:
    args = cli_main.build_parser().parse_args(
        ["network", "resolve", "localhost", "--timeout", "2", "--json"]
    )

    assert args.command == "network"
    assert args.network_command == "resolve"
    assert args.host == "localhost"
    assert args.timeout_seconds == 2.0
    assert args.as_json is True


def test_network_parser_exposes_tcp_contract() -> None:
    args = cli_main.build_parser().parse_args(["network", "tcp", "127.0.0.1", "8080", "--json"])

    assert args.command == "network"
    assert args.network_command == "tcp"
    assert args.host == "127.0.0.1"
    assert args.port == 8080
    assert args.as_json is True


def test_network_resolve_json_round_trip(capsys) -> None:
    result = cli_main.main(["network", "resolve", "localhost", "--timeout", "2", "--json"])

    assert result == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert payload["addresses"]


def test_network_tcp_json_round_trip(capsys) -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        port = listener.getsockname()[1]

        result = cli_main.main(
            ["network", "tcp", "127.0.0.1", str(port), "--timeout", "2", "--json"]
        )

    assert result == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["connected"] is True
    assert payload["peer_address"] == "127.0.0.1"


def test_network_invalid_input_returns_two(capsys) -> None:
    result = cli_main.main(["network", "tcp", "bad/path", "80", "--json"])

    assert result == 2
    assert "not a URL" in json.loads(capsys.readouterr().err)["error"]
