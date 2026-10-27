"""CLI for bounded local network probes."""

from __future__ import annotations

import json
import sys

from local_agent.host_ops.capabilities.local.network import NetworkProbeError, NetworkProber


def run_resolve(host: str, *, timeout_seconds: float, as_json: bool) -> int:
    try:
        result = NetworkProber().resolve(host, timeout_seconds=timeout_seconds)
    except NetworkProbeError as exc:
        return _render_input_error(exc, as_json=as_json)

    if as_json:
        print(json.dumps(result.as_dict(), sort_keys=True))
    elif result.ok:
        for address in result.addresses:
            print(f"{address.family} {address.address}")
    else:
        print(f"network resolution failed: {result.error}", file=sys.stderr)
    return 0 if result.ok else 1


def run_tcp(
    host: str,
    port: int,
    *,
    timeout_seconds: float,
    as_json: bool,
) -> int:
    try:
        result = NetworkProber().tcp(host, port, timeout_seconds=timeout_seconds)
    except NetworkProbeError as exc:
        return _render_input_error(exc, as_json=as_json)

    if as_json:
        print(json.dumps(result.as_dict(), sort_keys=True))
    elif result.ok:
        print(
            f"tcp connected host={result.host} port={result.port} "
            f"peer={result.peer_address} family={result.family}"
        )
    else:
        print(f"tcp probe failed: {result.error}", file=sys.stderr)
    return 0 if result.ok else 1


def _render_input_error(exc: NetworkProbeError, *, as_json: bool) -> int:
    if as_json:
        print(json.dumps({"error": str(exc)}, sort_keys=True), file=sys.stderr)
    else:
        print(f"network probe input invalid: {exc}", file=sys.stderr)
    return 2
