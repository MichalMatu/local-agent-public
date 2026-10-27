"""Offline loopback-only network Host Ops tests for physical Linux."""
from __future__ import annotations

import pathlib
import socket
import sys
import time
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from linux_agent.host_ops_network import NetworkProbeError, probe


class NetworkTests(unittest.TestCase):
    def test_dns_localhost(self):
        result = probe("resolve", "localhost", timeout=3)
        self.assertEqual(result["host"], "localhost")
        self.assertTrue(result["addresses"])
        self.assertLessEqual(len(result["addresses"]), 16)

    def test_one_loopback_tcp_success_and_closed_port(self):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
            server.bind(("127.0.0.1", 0))
            server.listen(1)
            port = server.getsockname()[1]
            good = probe("tcp", "127.0.0.1", port=port, timeout=3)
            self.assertTrue(good["connected"])
        bad = probe("tcp", "127.0.0.1", port=port, timeout=3)
        self.assertFalse(bad["connected"])
        self.assertIsNotNone(bad["failure"])

    def test_invalid_host_port_and_time_are_rejected(self):
        for host in ("https://example.com", "-fake", "user@host", "localhost:443",
                     "", "*", "has spaces"):
            with self.subTest(host=host), self.assertRaises(NetworkProbeError):
                probe("resolve", host)
        for port in (None, 0, 65536, True):
            with self.subTest(port=port), self.assertRaises(NetworkProbeError):
                probe("tcp", "127.0.0.1", port=port)
        with self.assertRaises(NetworkProbeError):
            probe("resolve", "localhost", port=22)
        with self.assertRaises(NetworkProbeError):
            probe("resolve", "localhost", timeout=0)

    def test_blocked_dns_worker_times_out_without_indefinite_wait(self):
        def hang(*_args, **_kwargs):
            time.sleep(3)
            return []

        with mock.patch("linux_agent.host_ops_network.socket.getaddrinfo", side_effect=hang):
            before = time.monotonic()
            with self.assertRaisesRegex(NetworkProbeError, "timed out"):
                probe("resolve", "localhost", timeout=0.1)
            self.assertLess(time.monotonic() - before, 2.0)


if __name__ == "__main__":
    unittest.main()
