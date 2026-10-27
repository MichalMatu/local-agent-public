"""Pairing is a user-approved capability, never an implicit Send grant."""

from __future__ import annotations

import http.client
import json
import stat
import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path

from local_agent.host_ops.capabilities.local.browser.chat_bridge_http import make_bridge_handler
from local_agent.host_ops.capabilities.local.browser.chat_bridge_pairing import BridgePairing
from local_agent.host_ops.capabilities.local.browser.chat_bridge_spool import ChatBridgeSpool

GOOD_ORIGIN = "chrome-extension://" + "a" * 32
OTHER_ORIGIN = "chrome-extension://" + "b" * 32


class LocalPairingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name) / "private"
        self.directory.mkdir(mode=0o700)
        self.key_file = self.directory / "bridge.key"
        self.confirmed = threading.Event()
        self.release = threading.Event()
        def approve(origin, code):
            self.assertEqual(origin, GOOD_ORIGIN)
            self.assertRegex(code, r"^[0-9]{6}$")
            self.confirmed.set()
            self.release.wait(timeout=1)
            return True
        self.pairing = BridgePairing(self.key_file, confirm=approve)
        self.spool = ChatBridgeSpool(self.directory / "spool.json")
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), make_bridge_handler(
            self.spool, pairing=self.pairing, pairing_only=True
        ))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.thread.join, 1)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def request(self, method, path, *, data=None, origin=GOOD_ORIGIN, host=None, headers=None):
        con = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=3)
        h = {"Origin": origin, **(headers or {})}
        if host:
            h["Host"] = host
        body = None if data is None else json.dumps(data)
        if body is not None:
            h["Content-Type"] = "application/json"
        con.request(method, path, body, headers=h)
        resp = con.getresponse()
        payload = resp.read()
        result = resp.status, json.loads(payload) if payload else {}, dict(resp.headers)
        con.close()
        return result

    def test_explicit_native_approval_once_creates_private_persistent_key(self):
        code, body, headers = self.request("POST", "/v1/pair/start", data={})
        self.assertEqual(code, 200)
        self.assertEqual(headers.get("Access-Control-Allow-Origin"), GOOD_ORIGIN)
        self.assertTrue(self.confirmed.wait(1))
        ticket = body["ticket"]
        self.assertRegex(body["code"], r"^[0-9]{6}$")
        self.assertNotIn("key", body)
        self.assertEqual(self.request("POST", "/v1/pair/finish", data={"ticket": ticket})[1]["status"], "pending")
        self.assertEqual(self.request("POST", "/v1/pair/start", data={})[0], 409)
        self.assertEqual(self.request("POST", "/v1/pair/finish", data={"ticket": ticket}, origin=OTHER_ORIGIN)[0], 400)
        self.assertFalse(self.key_file.exists())
        self.release.set()
        for _ in range(100):
            code, result, _ = self.request("POST", "/v1/pair/finish", data={"ticket": ticket})
            if result.get("status") == "paired":
                break
            time.sleep(.01)
        self.assertEqual(code, 200)
        self.assertEqual(result["status"], "paired")
        self.assertEqual(len(result["key"]), len(self.key_file.read_text().strip()))
        self.assertEqual(result["key"], self.key_file.read_text().strip())
        self.assertEqual(stat.S_IMODE(self.key_file.stat().st_mode), 0o600)
        self.assertEqual(self.request("POST", "/v1/pair/finish", data={"ticket": ticket})[0], 400)
        self.assertEqual(self.request("GET", "/v1/next", headers={"X-Local-Agent-Bridge-Key": result["key"]})[0], 403)
        code, state, _ = self.request("GET", "/v1/status", headers={"X-Local-Agent-Bridge-Key": result["key"]})
        self.assertEqual(code, 200)
        self.assertEqual(state["mode"], "pair_only")

    def test_untrusted_web_origin_dns_rebinding_and_preflight(self):
        self.assertEqual(self.request("POST", "/v1/pair/start", data={}, origin="https://evil.example")[0], 403)
        self.assertEqual(self.request("POST", "/v1/pair/start", data={}, host="evil.example")[0], 403)
        self.assertEqual(self.request("OPTIONS", "/v1/pair/start", origin="https://evil.example")[0], 403)
        code, _, headers = self.request("OPTIONS", "/v1/pair/start")
        self.assertEqual(code, 204)
        self.assertEqual(headers["Access-Control-Allow-Origin"], GOOD_ORIGIN)
        self.assertFalse(self.key_file.exists())

    def test_denied_and_expired_never_create_key(self):
        now = [100.]
        refused = BridgePairing(self.key_file, confirm=lambda *_: False, clock=lambda: now[0])
        started = refused.start(GOOD_ORIGIN)
        for _ in range(100):
            result = refused.finish(GOOD_ORIGIN, started["ticket"])
            if result["status"] == "denied":
                break
            time.sleep(.01)
        self.assertEqual(result["status"], "denied")
        self.assertFalse(self.key_file.exists())
        expired = BridgePairing(self.key_file, confirm=lambda *_: True, clock=lambda: now[0])
        started = expired.start(GOOD_ORIGIN)
        now[0] += 121
        self.assertEqual(expired.finish(GOOD_ORIGIN, started["ticket"])["status"], "expired")
        self.assertFalse(self.key_file.exists())

    def test_insecure_existing_key_is_rejected(self):
        self.key_file.write_text("A" * 48)
        self.key_file.chmod(0o644)
        with self.assertRaises(PermissionError):
            self.pairing.key()
        self.key_file.unlink()
        self.key_file.symlink_to(self.directory / "other")
        with self.assertRaises(PermissionError):
            self.pairing.key()


if __name__ == "__main__":
    unittest.main()
