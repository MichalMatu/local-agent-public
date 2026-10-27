from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from local_agent.development import live_flow, live_seed


PARENT_URL = "https://chatgpt.com/c/11111111-1111-4111-8111-111111111111"
PLAN_DIGEST = "sha256:" + "a" * 64
LAUNCH_NONCE = "b" * 32


class DevelopmentLiveFlowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name).resolve()
        self.common = ["--home", str(self.home)]

    def invoke(self, *args: str) -> tuple[int, dict, str]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = live_flow.main([*args, *self.common])
        payload = json.loads(stdout.getvalue()) if stdout.getvalue().strip() else {}
        return code, payload, stderr.getvalue()

    def assert_layout_is_pinned(self, argv: list[str]) -> None:
        self.assertEqual(argv[:4], ["python", "-m", live_flow.FLOW_MODULE, argv[3]])
        for flag in ("--home", "--root", "--checkout", "--production-checkout"):
            self.assertIn(flag, argv)
        home_index = argv.index("--home")
        self.assertEqual(Path(argv[home_index + 1]), self.home)

    def test_seed_performs_only_seed_and_points_to_prepare(self) -> None:
        seeded = {
            "workflow_id": live_seed.LIVE_SEED_WORKFLOW_ID,
            "request_id": live_seed.LIVE_SEED_REQUEST_ID,
            "request_digest": PLAN_DIGEST,
        }
        with patch.object(live_flow.live_seed, "seed_live_slice", return_value=seeded) as seed:
            code, payload, stderr = self.invoke(
                "seed",
                "--parent-conversation-url",
                PARENT_URL,
            )

        self.assertEqual(code, 0, stderr)
        self.assertEqual(payload["step"], "seed")
        self.assertEqual(payload["result"], seeded)
        self.assertEqual(payload["next_action"]["command"], "prepare")
        self.assert_layout_is_pinned(payload["next_action"]["argv"])
        seed.assert_called_once()

    def test_prepare_points_to_login_before_arm(self) -> None:
        prepared = {"plan_digest": PLAN_DIGEST, "plan": {"request": {}}}
        with patch.object(
            live_flow.live_slice,
            "prepare_live_slice",
            return_value=prepared,
        ) as prepare:
            code, payload, stderr = self.invoke("prepare")

        self.assertEqual(code, 0, stderr)
        self.assertEqual(payload["result"], prepared)
        self.assertEqual(payload["next_action"]["command"], "login")
        self.assert_layout_is_pinned(payload["next_action"]["argv"])
        args = prepare.call_args.args
        self.assertEqual(args[1:], (live_seed.LIVE_SEED_WORKFLOW_ID, live_seed.LIVE_SEED_REQUEST_ID))

    def test_successful_login_probe_points_to_exact_plan_digest_arm(self) -> None:
        with (
            patch.object(
                live_flow.live_runner,
                "_probe_live_slice_browser_login",
                return_value={"ok": True, "reason": "chatgpt_ready"},
            ) as probe,
            patch.object(
                live_flow.live_slice,
                "load_prepared_live_slice",
                return_value={"plan_digest": PLAN_DIGEST, "plan": {}},
            ) as load,
        ):
            code, payload, stderr = self.invoke("login", "--login-timeout-seconds", "45")

        self.assertEqual(code, 0, stderr)
        self.assertFalse(payload["result"]["manual_login_used"])
        self.assertEqual(payload["next_action"]["command"], "arm")
        argv = payload["next_action"]["argv"]
        self.assertEqual(argv[argv.index("--plan-digest") + 1], PLAN_DIGEST)
        self.assert_layout_is_pinned(argv)
        probe.assert_called_once()
        self.assertEqual(
            probe.call_args.kwargs["timeout_seconds"],
            live_flow.live_runner.DEFAULT_LOGIN_PROBE_TIMEOUT_SECONDS,
        )
        load.assert_called_once()

    def test_failed_login_probe_is_non_interactive_and_does_not_offer_arm(self) -> None:
        with (
            patch.object(
                live_flow.live_runner,
                "_probe_live_slice_browser_login",
                return_value={"ok": False, "reason": "chatgpt_login_timeout"},
            ) as probe,
            patch.object(live_flow.live_slice, "load_prepared_live_slice") as load,
        ):
            code, payload, _stderr = self.invoke("login")

        self.assertEqual(code, 3)
        self.assertFalse(payload["result"]["manual_login_used"])
        self.assertNotIn("next_action", payload)
        probe.assert_called_once()
        load.assert_not_called()

    def test_attach_requires_explicit_child_url_and_has_no_next_effect(self) -> None:
        attached = {
            "status": "completed",
            "recovered": True,
            "resolution": "manual_attach",
            "child_conversation_url": "https://chatgpt.com/c/22222222-2222-4222-8222-222222222222",
        }
        with patch.object(
            live_flow.live_runner,
            "attach_ambiguous_live_slice",
            return_value=attached,
        ) as attach:
            code, payload, stderr = self.invoke(
                "attach",
                "--child-conversation-url",
                attached["child_conversation_url"],
            )

        self.assertEqual(code, 0, stderr)
        self.assertEqual(payload["result"], attached)
        self.assertNotIn("next_action", payload)
        attach.assert_called_once()

    def test_arm_points_to_run_with_exact_nonce_and_timeout(self) -> None:
        arm = {
            "schema_version": 1,
            "plan_digest": PLAN_DIGEST,
            "launch_nonce": LAUNCH_NONCE,
            "armed_at": "2026-09-24T21:00:00Z",
            "expires_at": "2026-09-24T21:05:00Z",
        }
        with patch.object(live_flow.live_slice, "arm_live_slice", return_value=arm) as arm_call:
            code, payload, stderr = self.invoke(
                "arm",
                "--plan-digest",
                PLAN_DIGEST,
                "--ttl-seconds",
                "300",
                "--login-timeout-seconds",
                "45",
            )

        self.assertEqual(code, 0, stderr)
        self.assertEqual(payload["next_action"]["command"], "run")
        argv = payload["next_action"]["argv"]
        self.assertEqual(argv[argv.index("--launch-nonce") + 1], LAUNCH_NONCE)
        self.assertEqual(argv[argv.index("--login-timeout-seconds") + 1], "45")
        self.assert_layout_is_pinned(argv)
        self.assertEqual(arm_call.call_args.kwargs["expected_plan_digest"], PLAN_DIGEST)
        self.assertEqual(arm_call.call_args.kwargs["ttl_seconds"], 300)

    def test_paused_run_requires_login_then_rearm_not_replay(self) -> None:
        paused = {
            "status": "paused",
            "reason": "chatgpt_login_timeout",
            "needs_rearm": True,
        }
        with patch.object(live_flow.live_runner, "run_live_slice", return_value=paused) as run:
            code, payload, _stderr = self.invoke(
                "run",
                "--launch-nonce",
                LAUNCH_NONCE,
                "--login-timeout-seconds",
                "45",
            )

        self.assertEqual(code, 3)
        self.assertEqual(payload["next_action"]["command"], "login")
        argv = payload["next_action"]["argv"]
        self.assertEqual(argv[argv.index("--login-timeout-seconds") + 1], "45")
        self.assert_layout_is_pinned(argv)
        self.assertEqual(run.call_args.kwargs["launch_nonce"], LAUNCH_NONCE)

    def test_manual_attach_required_run_has_no_automatic_next_effect(self) -> None:
        manual = {
            "status": "manual_attach_required",
            "reason": "spawn_create_recovery_missing",
            "needs_rearm": True,
        }
        with patch.object(live_flow.live_runner, "run_live_slice", return_value=manual):
            code, payload, _stderr = self.invoke(
                "run",
                "--launch-nonce",
                LAUNCH_NONCE,
                "--login-timeout-seconds",
                "45",
            )

        self.assertEqual(code, 3)
        self.assertEqual(payload["result"], manual)
        self.assertNotIn("next_action", payload)

    def test_completed_run_has_no_next_effect(self) -> None:
        completed = {"status": "completed", "recovered": False}
        with patch.object(live_flow.live_runner, "run_live_slice", return_value=completed):
            code, payload, stderr = self.invoke("run", "--launch-nonce", LAUNCH_NONCE)

        self.assertEqual(code, 0, stderr)
        self.assertEqual(payload["result"], completed)
        self.assertNotIn("next_action", payload)


if __name__ == "__main__":
    unittest.main()
