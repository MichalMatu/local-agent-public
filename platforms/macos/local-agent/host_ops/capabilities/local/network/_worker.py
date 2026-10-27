"""Isolated stdlib network worker executed under the shared bounded process runner."""

from __future__ import annotations

import json
import socket
import sys
from collections.abc import Mapping, Sequence


def main(argv: Sequence[str] | None = None) -> int:
    args = tuple(sys.argv[1:] if argv is None else argv)
    if not args:
        return _emit_error("missing network worker operation")
    operation = args[0]
    if operation == "resolve" and len(args) == 2:
        return _resolve(args[1])
    if operation == "tcp" and len(args) == 4:
        try:
            port = int(args[2])
            timeout_seconds = float(args[3])
        except ValueError:
            return _emit_error("invalid tcp worker arguments")
        return _tcp(args[1], port, timeout_seconds)
    return _emit_error("invalid network worker arguments")


def _resolve(host: str) -> int:
    try:
        infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except OSError as exc:
        return _emit({"addresses": [], "error": f"{type(exc).__name__}: {exc}"})

    addresses: set[tuple[str, str]] = set()
    for family, _socktype, _proto, _canonname, sockaddr in infos:
        label = _family_name(family)
        if label is None:
            continue
        addresses.add((label, str(sockaddr[0])))
    payload = {
        "addresses": [
            {"family": family, "address": address} for family, address in sorted(addresses)
        ],
        "error": None if addresses else "resolver returned no IPv4 or IPv6 addresses",
    }
    return _emit(payload)


def _tcp(host: str, port: int, timeout_seconds: float) -> int:
    try:
        with socket.create_connection((host, port), timeout=timeout_seconds) as connection:
            peer = connection.getpeername()
            family = _family_name(connection.family)
            return _emit(
                {
                    "connected": True,
                    "peer_address": str(peer[0]),
                    "family": family,
                    "error": None,
                }
            )
    except OSError as exc:
        return _emit(
            {
                "connected": False,
                "peer_address": None,
                "family": None,
                "error": f"{type(exc).__name__}: {exc}",
            }
        )


def _family_name(family: int) -> str | None:
    if family == socket.AF_INET:
        return "ipv4"
    if family == socket.AF_INET6:
        return "ipv6"
    return None


def _emit(payload: Mapping[str, object]) -> int:
    print(json.dumps(payload, sort_keys=True))
    return 0


def _emit_error(message: str) -> int:
    print(json.dumps({"error": message}, sort_keys=True))
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
