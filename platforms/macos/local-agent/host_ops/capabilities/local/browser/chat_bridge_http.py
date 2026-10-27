"""Opt-in loopback adapter for the GitHub-first Chat Bridge transport.

No daemon lifecycle or scheduling is owned here. Start explicitly with an
external supervisor; shutdown/restart never resets the durable spool.
"""

from __future__ import annotations

import hmac
import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from .chat_bridge_pairing import BridgePairing, valid_extension_origin
from .chat_bridge_spool import ChatBridgeSpool, save_parent_url, validate_parent_url

_KEY = re.compile(r"[A-Za-z0-9_-]{32,128}\Z")
_MAX_BODY = 4096


def _receive_parent_url(spool: ChatBridgeSpool, headers: Any, stream: Any) -> str:
    if headers.get("Content-Type", "").split(";")[0].strip() != "application/json":
        raise ValueError("invalid parent content type")
    length = int(headers.get("Content-Length", ""))
    if not 0 < length <= 1024:
        raise ValueError("invalid parent request size")
    body = json.loads(stream.read(length).decode("utf-8"))
    if not isinstance(body, dict) or set(body) != {"parent_url"}:
        raise ValueError("invalid parent request body")
    parent = validate_parent_url(body["parent_url"])
    save_parent_url(spool.path.with_suffix(".parent.json"), parent)
    return parent


def make_bridge_handler(spool: ChatBridgeSpool, shared_key: str = "", *,
                        pairing: BridgePairing | None = None,
                        pairing_only: bool = False) -> type[BaseHTTPRequestHandler]:
    if shared_key and (not isinstance(shared_key, str) or not _KEY.fullmatch(shared_key)):
        raise ValueError("Invalid private Bridge pairing key")
    if not pairing_only and not shared_key and pairing is None:
        raise ValueError("Bridge transport requires a pairing key")
    if pairing_only and pairing is None:
        raise ValueError("Pairing-only server requires pairing authority")

    class BridgeHandler(BaseHTTPRequestHandler):
        server_version = "LocalAgentChatBridge/1"
        sys_version = ""

        def log_message(self, _format: str, *_args: Any) -> None:
            # No URL, credential or prompt data in logs.
            pass

        def _origin(self) -> str:
            origin = self.headers.get("Origin", "")
            return origin if valid_extension_origin(origin) else ""

        def _send(self, status: int, result: dict[str, Any], *, cors: bool = False) -> None:
            data = json.dumps(result, separators=(",", ":")).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            if cors and self._origin():
                self.send_header("Access-Control-Allow-Origin", self._origin())
                self.send_header("Vary", "Origin")
            self.send_header("Content-Length", str(len(data)))
            # No wildcard CORS: only privileged extension fetches this endpoint.
            self.end_headers()
            self.wfile.write(data)

        def _key_matches(self) -> bool:
            current_key = pairing.key() if pairing is not None else shared_key
            return bool(current_key) and hmac.compare_digest(
                self.headers.get("X-Local-Agent-Bridge-Key", ""), current_key
            )

        def _authorized(self) -> bool:
            return not pairing_only and self._key_matches()

        def _local_request(self) -> bool:
            # Reject DNS rebinding and requests routed via alternate Host names.
            return self.headers.get("Host", "") == f"127.0.0.1:{self.server.server_port}"

        def do_OPTIONS(self) -> None:
            if not self._local_request() or not self._origin() or self.path not in {
                    "/v1/pair/start", "/v1/pair/finish", "/v1/status", "/v1/next",
                    "/v1/ack", "/v1/wake-ack", "/v1/parent"}:
                self._send(403, {"ok": False})
                return
            self.send_response(204)
            self.send_header("Access-Control-Allow-Origin", self._origin())
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Local-Agent-Bridge-Key")
            self.send_header("Access-Control-Max-Age", "60")
            self.send_header("Vary", "Origin")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()

        def do_GET(self) -> None:
            if not self._local_request():
                self._send(403, {"ok": False})
                return
            if pairing_only and self.path == "/v1/status" and self._key_matches():
                self._send(200, {"ok": True, "mode": "pair_only", "items": {}}, cors=True)
                return
            if not self._authorized():
                self._send(403, {"ok": False, "reason": "not_authorized"}, cors=True)
                return
            if self.path == "/v1/next":
                launch = spool.take()
                self._send(200, {"ok": True, "launch": launch}, cors=True)
            elif self.path == "/v1/status":
                self._send(200, {"ok": True, "items": spool.status()}, cors=True)
            else:
                self._send(404, {"ok": False})

        def do_POST(self) -> None:
            if not self._local_request():
                self._send(403, {"ok": False})
                return
            if self.path in {"/v1/pair/start", "/v1/pair/finish"}:
                if pairing is None or not self._origin():
                    self._send(403, {"ok": False, "reason": "pairing_unavailable"})
                    return
                try:
                    if self.headers.get("Content-Type", "").split(";")[0].strip() != "application/json":
                        raise ValueError("invalid pairing content type")
                    length = int(self.headers.get("Content-Length", ""))
                    if not 0 < length <= 512:
                        raise ValueError("invalid pairing size")
                    body = json.loads(self.rfile.read(length).decode("utf-8"))
                    if self.path == "/v1/pair/start":
                        if body != {}:
                            raise ValueError("invalid pairing start body")
                        result = {"ok": True, **pairing.start(self._origin())}
                    else:
                        if not isinstance(body, dict) or set(body) != {"ticket"}:
                            raise ValueError("invalid pairing finish body")
                        result = pairing.finish(self._origin(), body["ticket"])
                except RuntimeError:
                    self._send(409, {"ok": False, "reason": "pairing_busy"}, cors=True)
                    return
                except (ValueError, TypeError, PermissionError, OSError, UnicodeDecodeError):
                    self._send(400, {"ok": False, "reason": "pairing_rejected"}, cors=True)
                    return
                self._send(200, result, cors=True)
                return
            if not self._authorized():
                self._send(403, {"ok": False, "reason": "not_authorized"}, cors=True)
                return
            if self.path == "/v1/parent":
                try:
                    parent = _receive_parent_url(spool, self.headers, self.rfile)
                except (ValueError, TypeError, UnicodeDecodeError, OSError):
                    self._send(400, {"ok": False, "reason": "parent_invalid"}, cors=True)
                    return
                self._send(200, {"ok": True, "parent_url": parent}, cors=True)
                return
            if self.path not in {"/v1/ack", "/v1/wake-ack"}:
                self._send(404, {"ok": False})
                return
            try:
                length = int(self.headers.get("Content-Length", ""))
                if not 0 < length <= _MAX_BODY:
                    raise ValueError("invalid size")
                body = json.loads(self.rfile.read(length).decode("utf-8"))
                if self.path == "/v1/wake-ack":
                    if not isinstance(body, dict) or set(body) != {"wake_id"}:
                        raise ValueError("invalid wake body")
                    status = spool.acknowledge_parent_wake(body["wake_id"])
                else:
                    if not isinstance(body, dict) or set(body) != {"transaction_id", "child_url"}:
                        raise ValueError("invalid body")
                    status = spool.acknowledge(body["transaction_id"], body["child_url"])
            except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
                self._send(400, {"ok": False, "reason": "invalid_ack"})
                return
            self._send(200, {"ok": True, "status": status}, cors=True)

    return BridgeHandler


def serve_bridge(spool: ChatBridgeSpool, shared_key: str = "", *, port: int = 8768,
                 pairing: BridgePairing | None = None,
                 pairing_only: bool = False) -> None:
    if not isinstance(port, int) or not 1024 <= port <= 65535:
        raise ValueError("Invalid Bridge localhost port")
    handler = make_bridge_handler(spool, shared_key, pairing=pairing,
                                  pairing_only=pairing_only)
    with ThreadingHTTPServer(("127.0.0.1", port), handler) as server:
        server.serve_forever(poll_interval=0.25)
