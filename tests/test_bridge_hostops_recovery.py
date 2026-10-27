from __future__ import annotations

import hashlib
import importlib.util
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = ROOT / "scripts" / "bridge_hostops_recovery.py"
SPEC = importlib.util.spec_from_file_location("bridge_hostops_recovery", SCRIPT_PATH)
assert SPEC is not None and SPEC.loader is not None
bridge_recovery = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(bridge_recovery)


class BridgeHostOpsRecoveryTests(unittest.TestCase):
    def test_normalize_conversation_url_accepts_exact_chat_url(self) -> None:
        self.assertEqual(
            bridge_recovery.normalize_conversation_url("https://chatgpt.com/c/example"),
            "https://chatgpt.com/c/example",
        )
        self.assertEqual(
            bridge_recovery.normalize_conversation_url("https://CHAT.OPENAI.COM/c/example"),
            "https://chat.openai.com/c/example",
        )

    def test_normalize_conversation_url_rejects_unsafe_or_ambiguous_forms(self) -> None:
        for value in (
            "http://chatgpt.com/c/example",
            "https://example.com/c/example",
            "https://user@chatgpt.com/c/example",
            "https://chatgpt.com:443/c/example",
            "https://chatgpt.com/c/example?token=private",
            "https://chatgpt.com/c/example#fragment",
            "https://chatgpt.com/",
        ):
            with self.subTest(value=value):
                with self.assertRaises(bridge_recovery.BridgeHostOpsRecoveryError):
                    bridge_recovery.normalize_conversation_url(value)

    def test_composer_selector_covers_legacy_and_current_chatgpt_dom(self) -> None:
        selector = bridge_recovery._COMPOSER_SELECTOR
        self.assertIn("#prompt-textarea", selector)
        self.assertIn('[data-testid="prompt-textarea"]', selector)
        self.assertIn('div.ProseMirror[contenteditable="true"]', selector)

    def test_fingerprints_follow_manifest_content_script_order(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "manifest.json").write_text(
                json.dumps(
                    {
                        "content_scripts": [
                            {
                                "matches": ["https://chatgpt.com/*"],
                                "js": ["control_protocol.js", "content.js"],
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            (root / "control_protocol.js").write_text("const version = 7;\n", encoding="utf-8")
            (root / "content.js").write_text("globalThis.bridge = true;\n", encoding="utf-8")

            fingerprints = bridge_recovery.bridge_content_script_fingerprints(root)
            self.assertEqual([name for name, _digest in fingerprints], ["control_protocol.js", "content.js"])
            self.assertEqual(
                fingerprints[0][1],
                hashlib.sha256((root / "control_protocol.js").read_bytes()).hexdigest(),
            )
            self.assertEqual(
                fingerprints[1][1],
                hashlib.sha256((root / "content.js").read_bytes()).hexdigest(),
            )

    def test_fingerprints_fail_closed_on_duplicate_basename_or_escape(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "a").mkdir()
            (root / "b").mkdir()
            (root / "a" / "content.js").write_text("a", encoding="utf-8")
            (root / "b" / "content.js").write_text("b", encoding="utf-8")
            (root / "manifest.json").write_text(
                json.dumps(
                    {
                        "content_scripts": [
                            {
                                "matches": ["https://chatgpt.com/*"],
                                "js": ["a/content.js", "b/content.js"],
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(
                bridge_recovery.BridgeHostOpsRecoveryError,
                "basenames must be unique",
            ):
                bridge_recovery.bridge_content_script_fingerprints(root)

            outside = root.parent / "outside.js"
            outside.write_text("outside", encoding="utf-8")
            (root / "manifest.json").write_text(
                json.dumps(
                    {
                        "content_scripts": [
                            {
                                "matches": ["https://chatgpt.com/*"],
                                "js": ["../outside.js"],
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            try:
                with self.assertRaisesRegex(
                    bridge_recovery.BridgeHostOpsRecoveryError,
                    "escapes chat_bridge",
                ):
                    bridge_recovery.bridge_content_script_fingerprints(root)
            finally:
                outside.unlink(missing_ok=True)

    def test_select_exact_page_target_requires_one_exact_page(self) -> None:
        payload = {
            "targets": [
                {"id": "page-1", "type": "page", "url": "https://chatgpt.com/c/a"},
                {"id": "worker-1", "type": "service_worker", "url": "chrome-extension:"},
            ]
        }
        self.assertEqual(
            bridge_recovery.select_exact_page_target(payload, "https://chatgpt.com/c/a"),
            "page-1",
        )

        with self.assertRaisesRegex(bridge_recovery.BridgeHostOpsRecoveryError, "no exact"):
            bridge_recovery.select_exact_page_target(payload, "https://chatgpt.com/c/missing")

        payload["targets"].append(
            {"id": "page-2", "type": "page", "url": "https://chatgpt.com/c/a"}
        )
        with self.assertRaisesRegex(bridge_recovery.BridgeHostOpsRecoveryError, "multiple"):
            bridge_recovery.select_exact_page_target(payload, "https://chatgpt.com/c/a")

    def test_external_check_defaults_to_read_only_readiness(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            bridge_dir = self._bridge_fixture(Path(temporary))
            calls: list[list[str]] = []

            def runner(argv, **_kwargs):
                calls.append(list(argv))
                if len(calls) == 1:
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
                    payload = {
                        "target_id": "page-1",
                        "dom_ready": True,
                        "content_script_state": "ready",
                        "worker_state": "inactive",
                        "diagnosis": "worker_inactive",
                    }
                return subprocess.CompletedProcess(argv, 0, json.dumps(payload), "")

            result = bridge_recovery.external_bridge_check(
                hostops="/fake/hostops",
                endpoint="http://127.0.0.1:9222",
                conversation_url="https://chatgpt.com/c/a",
                recover=False,
                timeout_seconds=30,
                bridge_dir=bridge_dir,
                runner=runner,
            )

            self.assertEqual(result["mode"], "inspect")
            self.assertEqual(result["target_id"], "page-1")
            self.assertEqual(result["fingerprinted_scripts"], ["content.js", "exhaustion_guard.js"])
            self.assertEqual(result["result"]["diagnosis"], "worker_inactive")
            self.assertIn("readiness", calls[1])
            self.assertNotIn("recover-content-script", calls[1])
            self.assertNotIn("--expect-url", calls[1])
            self.assertEqual(
                calls[1][calls[1].index("--selector") + 1],
                bridge_recovery._COMPOSER_SELECTOR,
            )

    def test_external_recovery_uses_exact_url_guard_and_all_fingerprints(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            bridge_dir = self._bridge_fixture(Path(temporary))
            calls: list[list[str]] = []

            def runner(argv, **_kwargs):
                calls.append(list(argv))
                if len(calls) == 1:
                    payload = {
                        "targets": [
                            {
                                "id": "page-9",
                                "type": "page",
                                "url": "https://chatgpt.com/c/a",
                            }
                        ]
                    }
                elif len(calls) == 2:
                    payload = {
                        "target_id": "page-9",
                        "target_type": "page",
                        "selectors": [
                            {
                                "selector": bridge_recovery._ACTIVE_STREAM_SELECTOR,
                                "match_count": 0,
                            }
                        ],
                    }
                else:
                    payload = {
                        "target_id": "page-9",
                        "action": "reload",
                        "outcome": "recovered",
                    }
                return subprocess.CompletedProcess(argv, 0, json.dumps(payload), "")

            result = bridge_recovery.external_bridge_check(
                hostops="/fake/hostops",
                endpoint="http://127.0.0.1:9222",
                conversation_url="https://chatgpt.com/c/a",
                recover=True,
                timeout_seconds=25,
                bridge_dir=bridge_dir,
                runner=runner,
            )

            preflight = calls[1]
            command = calls[2]
            self.assertEqual(result["mode"], "recover")
            self.assertEqual(result["result"]["outcome"], "recovered")
            self.assertIn("selectors", preflight)
            self.assertEqual(
                preflight[preflight.index("--selector") + 1],
                bridge_recovery._ACTIVE_STREAM_SELECTOR,
            )
            self.assertIn("recover-content-script", command)
            self.assertEqual(command[command.index("--expect-url") + 1], "https://chatgpt.com/c/a")
            self.assertEqual(command.count("--script-fingerprint"), 2)
            self.assertIn("--selector", command)
            self.assertEqual(
                command[command.index("--selector") + 1],
                bridge_recovery._COMPOSER_SELECTOR,
            )

    def test_external_recovery_refuses_while_generation_control_is_present(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            bridge_dir = self._bridge_fixture(Path(temporary))
            calls: list[list[str]] = []

            def runner(argv, **_kwargs):
                calls.append(list(argv))
                if len(calls) == 1:
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
                    payload = {
                        "target_id": "page-1",
                        "target_type": "page",
                        "selectors": [
                            {
                                "selector": bridge_recovery._ACTIVE_STREAM_SELECTOR,
                                "match_count": 1,
                            }
                        ],
                    }
                return subprocess.CompletedProcess(argv, 0, json.dumps(payload), "")

            with self.assertRaisesRegex(
                bridge_recovery.BridgeHostOpsRecoveryError,
                "refusing recovery",
            ):
                bridge_recovery.external_bridge_check(
                    hostops="/fake/hostops",
                    endpoint="http://127.0.0.1:9222",
                    conversation_url="https://chatgpt.com/c/a",
                    recover=True,
                    timeout_seconds=30,
                    bridge_dir=bridge_dir,
                    runner=runner,
                )
            self.assertEqual(len(calls), 2)
            self.assertIn("selectors", calls[1])

    def test_external_recovery_force_skips_generation_preflight(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            bridge_dir = self._bridge_fixture(Path(temporary))
            calls: list[list[str]] = []

            def runner(argv, **_kwargs):
                calls.append(list(argv))
                payload = (
                    {
                        "targets": [
                            {
                                "id": "page-1",
                                "type": "page",
                                "url": "https://chatgpt.com/c/a",
                            }
                        ]
                    }
                    if len(calls) == 1
                    else {"target_id": "page-1", "action": "reload", "outcome": "recovered"}
                )
                return subprocess.CompletedProcess(argv, 0, json.dumps(payload), "")

            result = bridge_recovery.external_bridge_check(
                hostops="/fake/hostops",
                endpoint="http://127.0.0.1:9222",
                conversation_url="https://chatgpt.com/c/a",
                recover=True,
                timeout_seconds=30,
                force=True,
                bridge_dir=bridge_dir,
                runner=runner,
            )
            self.assertEqual(result["result"]["outcome"], "recovered")
            self.assertEqual(len(calls), 2)
            self.assertNotIn("selectors", calls[1])

    def test_hostops_failure_is_not_retried_or_masked(self) -> None:
        calls = 0

        def runner(argv, **_kwargs):
            nonlocal calls
            calls += 1
            return subprocess.CompletedProcess(argv, 2, "", '{"error":"endpoint unavailable"}')

        with self.assertRaisesRegex(bridge_recovery.BridgeHostOpsRecoveryError, "exit 2"):
            bridge_recovery.external_bridge_check(
                hostops="/fake/hostops",
                endpoint="http://127.0.0.1:9222",
                conversation_url="https://chatgpt.com/c/a",
                recover=True,
                timeout_seconds=30,
                runner=runner,
            )
        self.assertEqual(calls, 1)

    @staticmethod
    def _bridge_fixture(root: Path) -> Path:
        (root / "manifest.json").write_text(
            json.dumps(
                {
                    "content_scripts": [
                        {
                            "matches": ["https://chatgpt.com/*", "https://chat.openai.com/*"],
                            "js": ["content.js", "exhaustion_guard.js"],
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        (root / "content.js").write_text("globalThis.content = true;\n", encoding="utf-8")
        (root / "exhaustion_guard.js").write_text("globalThis.guard = true;\n", encoding="utf-8")
        return root


if __name__ == "__main__":
    unittest.main()
