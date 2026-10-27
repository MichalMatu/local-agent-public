"""On-demand bounded DNS and one-endpoint TCP diagnostics on Linux.

DNS runs in a disposable child because libc name resolution does not promise a
per-call timeout. The existing Agent command budget remains the outer fence.
This module does not continuously poll, discover hosts or open listening ports.
"""
from __future__ import annotations

import ipaddress
import multiprocessing
import re
import socket
import time

_DNS_NAME = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9._-]{0,252})$")


class NetworkProbeError(ValueError):
    """Invalid, timed-out or unavailable one-endpoint network probe."""


def _target(host: str, timeout: float) -> None:
    if not isinstance(host, str) or len(host) > 253 or any(c.isspace() for c in host):
        raise NetworkProbeError("explicit DNS name or IP address required")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        if not _DNS_NAME.fullmatch(host) or host.startswith("-"):
            raise NetworkProbeError("invalid host: no URL, credentials, wildcard or port")
    if not 0 < timeout <= 30:
        raise NetworkProbeError("network timeout must be in (0, 30] seconds")


def _child(sender, kind: str, host: str, port: int | None, timeout: float) -> None:
    try:
        if kind == "resolve":
            info = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
            values = sorted(set(row[4][0] for row in info))
            data = {"host": host, "addresses": values[:16],
                    "addresses_truncated": len(values) > 16}
        elif kind == "tcp":
            assert port is not None
            try:
                with socket.create_connection((host, port), timeout=timeout):
                    data = {"host": host, "port": port, "connected": True,
                            "failure": None}
            except OSError as exc:
                data = {"host": host, "port": port, "connected": False,
                        "failure": type(exc).__name__}
        else:
            raise ValueError("invalid worker action")
        sender.send({"ok": True, "data": data})
    except (OSError, ValueError, AssertionError) as exc:
        sender.send({"ok": False, "error": type(exc).__name__})
    finally:
        sender.close()


def probe(kind: str, host: str, *, port: int | None = None,
          timeout: float = 5.0) -> dict:
    _target(host, timeout)
    if kind not in {"resolve", "tcp"}:
        raise NetworkProbeError("unsupported network action")
    if kind == "tcp" and (type(port) is not int or not 1 <= port <= 65535):
        raise NetworkProbeError("explicit TCP port must be in 1..65535")
    if kind == "resolve" and port is not None:
        raise NetworkProbeError("DNS resolution does not take a port")
    context = multiprocessing.get_context("fork")
    receiver, sender = context.Pipe(duplex=False)
    worker = context.Process(target=_child, args=(sender, kind, host, port, timeout), daemon=True)
    try:
        worker.start()
        sender.close()
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise NetworkProbeError("network probe timed out")
            if receiver.poll(min(remaining, 0.1)):
                try:
                    message = receiver.recv()
                except EOFError as exc:
                    raise NetworkProbeError("network worker exited without result") from exc
                if not isinstance(message, dict) or not message.get("ok"):
                    raise NetworkProbeError("network probe failed: " + str(message.get("error")))
                return message["data"]
            if not worker.is_alive():
                raise NetworkProbeError("network worker exited without result")
    finally:
        receiver.close()
        sender.close()
        if worker.pid is not None:
            if worker.is_alive():
                worker.terminate()
            worker.join(timeout=0.5)
            if worker.is_alive():
                worker.kill()
                worker.join(timeout=0.5)
