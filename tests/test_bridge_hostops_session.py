from __future__ import annotations

import importlib.util
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = ROOT / "scripts" / "bridge_hostops_session.py"
SPEC = importlib.util.spec_from_file_location("bridge_hostops_session", SCRIPT_PATH)
assert SPEC is not None and SPEC.loader is not None
bridge_session = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(bridge_session)


class BridgeHostOpsSessionTests(unittest.TestCase):
    def test_normalize_start_url_accepts_approved_chat_hosts(self) -> None:
        self.assertEqual(
            bridge_session.normalize_start_url("https://chatgpt.com/"),
            "https://chatgpt.com/",
        )
        self.assertEqual(
            bridge_session.normalize_start_url("https://CHAT.OPENAI.COM/c/example"),
            "https://chat.openai.com/c/example",
        )

    def test_normalize_start_url_rejects_private_or_ambiguous_forms(self) -> None:
        for value in (
            "http://chatgpt.com/",
            "https://example.com/",
            "https://user@chatgpt.com/",
            "https://chatgpt.com:443/",
            "https://chatgpt.com/?token=private",
            "https://chatgpt.com/#fragment",
        ):
            with self.subTest(value=value):
                with self.assertRaises(bridge_session.BridgeHostOpsSessionError):
                    bridge_session.normalize_start_url(value)

    def test_start_delegates_exact_bridge_profile_extension_and_url_to_hostops(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            bridge_dir = Path(temporary) / "chat_bridge"
            bridge_dir.mkdir()
            (bridge_dir / "manifest.json").write_text("{}\n", encoding="utf-8")
            calls: list[list[str]] = []

            def runner(argv, **kwargs):
                calls.append(list(argv))
                self.assertEqual(kwargs["timeout"], 25)
                payload = {
                    "state": "running",
                    "pid": 4321,
                    "endpoint": "http://127.0.0.1:54321",
                    "browser": "Chrome/153.0.8010.36",
                }
                return subprocess.CompletedProcess(argv, 0, json.dumps(payload), "")

            browser = (
                "/Applications/Google Chrome for Testing.app/Contents/MacOS/"
                "Google Chrome for Testing"
            )
            result = bridge_session.managed_bridge_session(
                action="start",
                hostops="/fake/hostops",
                profile_dir="/tmp/local-agent-chat-bridge",
                browser_executable=browser,
                start_url="https://chatgpt.com/c/example",
                timeout_seconds=20,
                bridge_dir=bridge_dir,
                runner=runner,
            )

            command = calls[0]
            self.assertEqual(command[:4], ["/fake/hostops", "browser", "session", "start"])
            self.assertEqual(
                command[command.index("--profile-dir") + 1],
                "/tmp/local-agent-chat-bridge",
            )
            self.assertEqual(
                command[command.index("--extension-dir") + 1],
                str(bridge_dir.resolve()),
            )
            self.assertEqual(
                command[command.index("--browser-executable") + 1],
                browser,
            )
            self.assertEqual(
                command[command.index("--url") + 1],
                "https://chatgpt.com/c/example",
            )
            self.assertEqual(command[-3:], ["--timeout", "20", "--json"])
            self.assertEqual(result["action"], "start")
            self.assertEqual(result["result"]["state"], "running")
            self.assertEqual(result["bridge_dir"], str(bridge_dir.resolve()))

    def test_login_start_uses_interactive_hostops_without_extension(self) -> None:
        calls: list[list[str]] = []
        browser = (
            "/Applications/Google Chrome for Testing.app/Contents/MacOS/"
            "Google Chrome for Testing"
        )

        def runner(argv, **_kwargs):
            calls.append(list(argv))
            payload = {"state": "running", "pid": 4321, "endpoint": None, "browser": "chrome"}
            return subprocess.CompletedProcess(argv, 0, json.dumps(payload), "")

        result = bridge_session.managed_bridge_session(
            action="login-start",
            hostops="/fake/hostops",
            profile_dir="/tmp/local-agent-chat-bridge",
            browser_executable=browser,
            start_url="https://chatgpt.com/",
            timeout_seconds=20,
            runner=runner,
        )

        command = calls[0]
        self.assertEqual(
            command[:4], ["/fake/hostops", "browser", "session", "interactive-start"]
        )
        self.assertIn("--browser-executable", command)
        self.assertIn("--url", command)
        self.assertNotIn("--extension-dir", command)
        self.assertEqual(result["action"], "login-start")
        self.assertNotIn("bridge_dir", result)

    def test_login_status_and_finish_map_to_interactive_lifecycle(self) -> None:
        calls: list[list[str]] = []

        def runner(argv, **_kwargs):
            calls.append(list(argv))
            state = "running" if argv[3] == "interactive-status" else "stopped"
            payload = {"state": state, "pid": 4321 if state == "running" else None, "endpoint": None, "browser": "chrome" if state == "running" else None}
            return subprocess.CompletedProcess(argv, 0, json.dumps(payload), "")

        bridge_session.managed_bridge_session(
            action="login-status",
            hostops="/fake/hostops",
            profile_dir="/tmp/profile",
            timeout_seconds=5,
            runner=runner,
        )
        bridge_session.managed_bridge_session(
            action="login-finish",
            hostops="/fake/hostops",
            profile_dir="/tmp/profile",
            timeout_seconds=10,
            runner=runner,
        )

        self.assertEqual(calls[0][3], "interactive-status")
        self.assertEqual(calls[1][3], "interactive-stop")
        self.assertIn("--clear-session-restore", calls[1])
        self.assertNotIn("--browser-executable", calls[1])
        self.assertNotIn("--extension-dir", calls[1])

    def test_interactive_running_payload_must_not_expose_cdp(self) -> None:
        def runner(argv, **_kwargs):
            payload = {
                "state": "running",
                "pid": 4321,
                "endpoint": "http://127.0.0.1:9222",
                "browser": "chrome",
            }
            return subprocess.CompletedProcess(argv, 0, json.dumps(payload), "")

        with self.assertRaisesRegex(
            bridge_session.BridgeHostOpsSessionError,
            "unexpectedly exposed CDP",
        ):
            bridge_session.managed_bridge_session(
                action="login-status",
                hostops="/fake/hostops",
                profile_dir="/tmp/profile",
                timeout_seconds=5,
                runner=runner,
            )

    def test_start_rejects_branded_macos_chrome_before_hostops(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            bridge_dir = Path(temporary) / "chat_bridge"
            bridge_dir.mkdir()
            (bridge_dir / "manifest.json").write_text("{}\n", encoding="utf-8")

            with self.assertRaisesRegex(
                bridge_session.BridgeHostOpsSessionError,
                "Chrome for Testing or Chromium",
            ):
                bridge_session.managed_bridge_session(
                    action="start",
                    hostops="/fake/hostops",
                    profile_dir="/tmp/local-agent-chat-bridge",
                    browser_executable=(
                        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
                    ),
                    timeout_seconds=20,
                    bridge_dir=bridge_dir,
                    runner=lambda *_args, **_kwargs: self.fail("runner must not be called"),
                )

    def test_status_and_stop_are_profile_scoped_only(self) -> None:
        calls: list[list[str]] = []
        payloads = {
            "status": {
                "state": "running",
                "pid": 4321,
                "endpoint": "http://127.0.0.1:54321",
                "browser": "Chrome/153.0.8010.36",
            },
            "stop": {"state": "stopped", "pid": None, "endpoint": None, "browser": None},
        }

        def runner(argv, **_kwargs):
            calls.append(list(argv))
            action = argv[3]
            return subprocess.CompletedProcess(argv, 0, json.dumps(payloads[action]), "")

        for action in ("status", "stop"):
            result = bridge_session.managed_bridge_session(
                action=action,
                hostops="/fake/hostops",
                profile_dir="/tmp/local-agent-chat-bridge",
                timeout_seconds=5,
                force=action == "stop",
                runner=runner,
            )
            self.assertEqual(result["action"], action)

        for command in calls:
            self.assertIn("--profile-dir", command)
            self.assertNotIn("--browser-executable", command)
            self.assertNotIn("--extension-dir", command)
            self.assertNotIn("--url", command)

    def test_stop_refuses_while_chatgpt_generation_control_is_present(self) -> None:
        calls: list[list[str]] = []

        def runner(argv, **_kwargs):
            calls.append(list(argv))
            if argv[1:4] == ["browser", "session", "status"]:
                payload = {
                    "state": "running",
                    "pid": 4321,
                    "endpoint": "http://127.0.0.1:54321",
                    "browser": "Chrome/153",
                }
            elif argv[1:4] == ["browser", "attach", "inspect"]:
                payload = {
                    "targets": [
                        {
                            "id": "page-1",
                            "type": "page",
                            "url": "https://chatgpt.com/c/a",
                        }
                    ]
                }
            else:
                self.assertIn("selectors", argv)
                payload = {
                    "target_id": "page-1",
                    "target_type": "page",
                    "selectors": [
                        {
                            "selector": bridge_session._ACTIVE_STREAM_SELECTOR,
                            "match_count": 1,
                        }
                    ],
                }
            return subprocess.CompletedProcess(argv, 0, json.dumps(payload), "")

        with self.assertRaisesRegex(
            bridge_session.BridgeHostOpsSessionError,
            "refusing to stop",
        ):
            bridge_session.managed_bridge_session(
                action="stop",
                hostops="/fake/hostops",
                profile_dir="/tmp/local-agent-chat-bridge",
                timeout_seconds=5,
                runner=runner,
            )
        self.assertEqual(len(calls), 3)
        self.assertFalse(any(call[1:4] == ["browser", "session", "stop"] for call in calls))

    def test_stop_proceeds_after_clear_generation_preflight(self) -> None:
        calls: list[list[str]] = []

        def runner(argv, **_kwargs):
            calls.append(list(argv))
            if argv[1:4] == ["browser", "session", "status"]:
                payload = {
                    "state": "running",
                    "pid": 4321,
                    "endpoint": "http://127.0.0.1:54321",
                    "browser": "Chrome/153",
                }
            elif argv[1:4] == ["browser", "attach", "inspect"]:
                payload = {
                    "targets": [
                        {
                            "id": "page-1",
                            "type": "page",
                            "url": "https://chatgpt.com/c/a",
                        }
                    ]
                }
            elif "selectors" in argv:
                payload = {
                    "target_id": "page-1",
                    "target_type": "page",
                    "selectors": [
                        {
                            "selector": bridge_session._ACTIVE_STREAM_SELECTOR,
                            "match_count": 0,
                        }
                    ],
                }
            else:
                payload = {"state": "stopped", "pid": None, "endpoint": None, "browser": None}
            return subprocess.CompletedProcess(argv, 0, json.dumps(payload), "")

        result = bridge_session.managed_bridge_session(
            action="stop",
            hostops="/fake/hostops",
            profile_dir="/tmp/local-agent-chat-bridge",
            timeout_seconds=5,
            runner=runner,
        )
        self.assertEqual(result["result"]["state"], "stopped")
        self.assertEqual(len(calls), 4)
        self.assertEqual(calls[-1][1:4], ["browser", "session", "stop"])

    def test_running_payload_requires_loopback_endpoint(self) -> None:
        def runner(argv, **_kwargs):
            payload = {
                "state": "running",
                "pid": 4321,
                "endpoint": "http://0.0.0.0:9222",
                "browser": "Chrome/153",
            }
            return subprocess.CompletedProcess(argv, 0, json.dumps(payload), "")

        with self.assertRaisesRegex(
            bridge_session.BridgeHostOpsSessionError,
            "loopback CDP endpoint",
        ):
            bridge_session.managed_bridge_session(
                action="status",
                hostops="/fake/hostops",
                profile_dir="/tmp/profile",
                timeout_seconds=5,
                runner=runner,
            )

    def test_hostops_failure_is_not_retried_or_masked(self) -> None:
        calls = 0

        def runner(argv, **_kwargs):
            nonlocal calls
            calls += 1
            return subprocess.CompletedProcess(argv, 2, "", '{"error":"profile guard failed"}')

        with self.assertRaisesRegex(bridge_session.BridgeHostOpsSessionError, "exit 2"):
            bridge_session.managed_bridge_session(
                action="status",
                hostops="/fake/hostops",
                profile_dir="/tmp/profile",
                timeout_seconds=5,
                runner=runner,
            )
        self.assertEqual(calls, 1)

    def test_start_requires_complete_bridge_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaisesRegex(
                bridge_session.BridgeHostOpsSessionError,
                "extension directory is incomplete",
            ):
                bridge_session.managed_bridge_session(
                    action="start",
                    hostops="/fake/hostops",
                    profile_dir="/tmp/profile",
                    browser_executable="/browser",
                    timeout_seconds=5,
                    bridge_dir=Path(temporary),
                    runner=lambda *_args, **_kwargs: self.fail("runner must not be called"),
                )


if __name__ == "__main__":
    unittest.main()
