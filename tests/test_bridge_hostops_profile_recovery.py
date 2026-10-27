from __future__ import annotations

import importlib.util
import json
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = ROOT / "scripts" / "bridge_hostops_recovery.py"
SPEC = importlib.util.spec_from_file_location("bridge_hostops_recovery_profile", SCRIPT_PATH)
assert SPEC is not None and SPEC.loader is not None
bridge_recovery = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(bridge_recovery)


class BridgeHostOpsProfileRecoveryTests(unittest.TestCase):
    def test_profile_resolution_uses_exact_managed_session_status(self) -> None:
        calls: list[list[str]] = []

        def runner(argv, **kwargs):
            calls.append(list(argv))
            self.assertEqual(kwargs["timeout"], 35)
            payload = {
                "state": "running",
                "pid": 4321,
                "endpoint": "http://127.0.0.1:54321",
                "browser": "Chrome/153.0.8010.54",
            }
            return subprocess.CompletedProcess(argv, 0, json.dumps(payload), "")

        endpoint = bridge_recovery.resolve_managed_profile_endpoint(
            hostops="/fake/hostops",
            profile_dir="/tmp/local-agent-chat-bridge",
            timeout_seconds=30,
            runner=runner,
        )

        self.assertEqual(endpoint, "http://127.0.0.1:54321")
        self.assertEqual(
            calls,
            [
                [
                    "/fake/hostops",
                    "browser",
                    "session",
                    "status",
                    "--profile-dir",
                    "/tmp/local-agent-chat-bridge",
                    "--timeout",
                    "30",
                    "--json",
                ]
            ],
        )

    def test_profile_resolution_rejects_stopped_or_unhealthy_session(self) -> None:
        for state in ("stopped", "unhealthy"):
            with self.subTest(state=state):

                def runner(argv, **_kwargs):
                    payload = {"state": state, "pid": None, "endpoint": None, "browser": None}
                    return subprocess.CompletedProcess(argv, 0, json.dumps(payload), "")

                with self.assertRaisesRegex(
                    bridge_recovery.BridgeHostOpsRecoveryError,
                    "profile is not running",
                ):
                    bridge_recovery.resolve_managed_profile_endpoint(
                        hostops="/fake/hostops",
                        profile_dir="/tmp/profile",
                        timeout_seconds=30,
                        runner=runner,
                    )

    def test_profile_resolution_requires_exact_loopback_endpoint(self) -> None:
        invalid_endpoints = (
            "http://0.0.0.0:9222",
            "https://127.0.0.1:9222",
            "http://127.0.0.1:9222/path",
            "http://127.0.0.1:9222?token=private",
            "http://127.0.0.1:9222#fragment",
            "http://user@127.0.0.1:9222",
            "http://127.0.0.1:123@evil.test",
            "http://127.0.0.1:not-a-port",
            None,
        )
        for endpoint in invalid_endpoints:
            with self.subTest(endpoint=endpoint):

                def runner(argv, **_kwargs):
                    payload = {
                        "state": "running",
                        "pid": 4321,
                        "endpoint": endpoint,
                        "browser": "Chrome/153",
                    }
                    return subprocess.CompletedProcess(argv, 0, json.dumps(payload), "")

                with self.assertRaisesRegex(
                    bridge_recovery.BridgeHostOpsRecoveryError,
                    "loopback CDP endpoint",
                ):
                    bridge_recovery.resolve_managed_profile_endpoint(
                        hostops="/fake/hostops",
                        profile_dir="/tmp/profile",
                        timeout_seconds=30,
                        runner=runner,
                    )

    def test_profile_resolution_rejects_empty_profile_before_hostops(self) -> None:
        with self.assertRaisesRegex(
            bridge_recovery.BridgeHostOpsRecoveryError,
            "profile path must not be empty",
        ):
            bridge_recovery.resolve_managed_profile_endpoint(
                hostops="/fake/hostops",
                profile_dir="   ",
                timeout_seconds=30,
                runner=lambda *_args, **_kwargs: self.fail("runner must not be called"),
            )

    def test_profile_resolution_propagates_hostops_failure_without_retry(self) -> None:
        calls = 0

        def runner(argv, **_kwargs):
            nonlocal calls
            calls += 1
            return subprocess.CompletedProcess(argv, 1, "", '{"error":"profile unavailable"}')

        with self.assertRaisesRegex(bridge_recovery.BridgeHostOpsRecoveryError, "exit 1"):
            bridge_recovery.resolve_managed_profile_endpoint(
                hostops="/fake/hostops",
                profile_dir="/tmp/profile",
                timeout_seconds=30,
                runner=runner,
            )
        self.assertEqual(calls, 1)

    def test_cli_requires_exactly_one_endpoint_source(self) -> None:
        base = ["--conversation-url", "https://chatgpt.com/c/example"]
        with self.assertRaises(SystemExit):
            bridge_recovery.parse_args(base)
        with self.assertRaises(SystemExit):
            bridge_recovery.parse_args(
                [
                    "--endpoint",
                    "http://127.0.0.1:9222",
                    "--profile-dir",
                    "/tmp/profile",
                    *base,
                ]
            )

        endpoint_args = bridge_recovery.parse_args(
            ["--endpoint", "http://127.0.0.1:9222", *base]
        )
        self.assertEqual(endpoint_args.endpoint, "http://127.0.0.1:9222")
        self.assertIsNone(endpoint_args.profile_dir)

        profile_args = bridge_recovery.parse_args(["--profile-dir", "/tmp/profile", *base])
        self.assertEqual(profile_args.profile_dir, "/tmp/profile")
        self.assertIsNone(profile_args.endpoint)


if __name__ == "__main__":
    unittest.main()
