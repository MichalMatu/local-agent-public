"""Guided operator CLI for the one bounded Conversation Fabric Stage 8 live slice.

Each invocation performs exactly one existing authority step. The wrapper only adds
machine-readable next-action guidance so the operator does not need to copy workflow
ids, plan digests or launch nonces by hand. It never chains effects automatically.
The ``login`` step is intentionally a bounded, non-interactive session probe; it never
opens a browser or waits for a human login.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Iterable

from local_agent.development import live_runner, live_seed, live_slice
from local_agent.development.lab import DevLabLayout, build_dev_lab_layout

FLOW_MODULE = "local_agent.development.live_flow"


def _path_arg(value: str | None) -> Path | None:
    return None if value is None else Path(value)


def _layout_from_args(args: argparse.Namespace) -> DevLabLayout:
    return build_dev_lab_layout(
        home=_path_arg(args.home),
        root=_path_arg(args.root),
        checkout=_path_arg(args.checkout),
        production_checkout=_path_arg(args.production_checkout),
    )


def _layout_argv(layout: DevLabLayout) -> list[str]:
    return [
        "--home",
        str(layout.home),
        "--root",
        str(layout.root),
        "--checkout",
        str(layout.checkout),
        "--production-checkout",
        str(layout.production_checkout),
    ]


def _next_action(
    layout: DevLabLayout,
    command: str,
    *extra: str,
) -> dict[str, Any]:
    return {
        "command": command,
        "argv": [
            "python",
            "-m",
            FLOW_MODULE,
            command,
            *extra,
            *_layout_argv(layout),
        ],
    }


def _output(
    *,
    step: str,
    result: dict[str, Any],
    next_action: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "step": step,
        "result": result,
    }
    if next_action is not None:
        payload["next_action"] = next_action
    return payload


def parse_args(argv: Iterable[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command",
        choices=("seed", "prepare", "login", "arm", "run", "attach", "status"),
    )
    parser.add_argument("--parent-conversation-url")
    parser.add_argument("--plan-digest")
    parser.add_argument("--launch-nonce")
    parser.add_argument("--child-conversation-url")
    parser.add_argument(
        "--ttl-seconds",
        type=int,
        default=live_slice.DEFAULT_ARM_TTL_SECONDS,
    )
    parser.add_argument(
        "--login-timeout-seconds",
        type=int,
        default=live_runner.DEFAULT_LOGIN_TIMEOUT_SECONDS,
    )
    parser.add_argument("--home")
    parser.add_argument("--root")
    parser.add_argument("--checkout")
    parser.add_argument("--production-checkout")
    return parser.parse_args(list(argv) if argv is not None else None)


def _run_step(args: argparse.Namespace, layout: DevLabLayout) -> tuple[dict[str, Any], int]:
    if args.command == "seed":
        if not args.parent_conversation_url:
            raise ValueError("--parent-conversation-url is required for seed")
        result = live_seed.seed_live_slice(
            layout,
            parent_conversation_url=str(args.parent_conversation_url),
        )
        return (
            _output(
                step="seed",
                result=result,
                next_action=_next_action(layout, "prepare"),
            ),
            0,
        )

    if args.command == "prepare":
        result = live_slice.prepare_live_slice(
            layout,
            live_seed.LIVE_SEED_WORKFLOW_ID,
            live_seed.LIVE_SEED_REQUEST_ID,
        )
        return (
            _output(
                step="prepare",
                result=result,
                next_action=_next_action(layout, "login"),
            ),
            0,
        )

    if args.command == "login":
        probe_timeout = min(
            args.login_timeout_seconds,
            live_runner.DEFAULT_LOGIN_PROBE_TIMEOUT_SECONDS,
        )
        result = live_runner._probe_live_slice_browser_login(
            layout,
            timeout_seconds=probe_timeout,
        )
        result = {**result, "manual_login_used": False}
        next_action = None
        if result.get("ok") is True:
            prepared = live_slice.load_prepared_live_slice(layout)
            digest = str(prepared["plan_digest"])
            next_action = _next_action(
                layout,
                "arm",
                "--plan-digest",
                digest,
            )
        return _output(step="login", result=result, next_action=next_action), (
            0 if result.get("ok") is True else 3
        )

    if args.command == "arm":
        if not args.plan_digest:
            raise ValueError("--plan-digest is required for arm")
        result = live_slice.arm_live_slice(
            layout,
            expected_plan_digest=str(args.plan_digest),
            ttl_seconds=args.ttl_seconds,
        )
        nonce = str(result["launch_nonce"])
        return (
            _output(
                step="arm",
                result=result,
                next_action=_next_action(
                    layout,
                    "run",
                    "--launch-nonce",
                    nonce,
                    "--login-timeout-seconds",
                    str(args.login_timeout_seconds),
                ),
            ),
            0,
        )

    if args.command == "attach":
        if not args.child_conversation_url:
            raise ValueError("--child-conversation-url is required for attach")
        result = live_runner.attach_ambiguous_live_slice(
            layout,
            child_conversation_url=str(args.child_conversation_url),
        )
        return _output(step="attach", result=result), 0

    if args.command == "run":
        if not args.launch_nonce:
            raise ValueError("--launch-nonce is required for run")
        result = live_runner.run_live_slice(
            layout,
            launch_nonce=str(args.launch_nonce),
            login_timeout_seconds=args.login_timeout_seconds,
        )
        next_action = None
        if (
            result.get("status") != "completed"
            and result.get("status") != "manual_attach_required"
            and result.get("needs_rearm") is True
        ):
            next_action = _next_action(
                layout,
                "login",
                "--login-timeout-seconds",
                str(args.login_timeout_seconds),
            )
        return _output(step="run", result=result, next_action=next_action), (
            0 if result.get("status") == "completed" else 3
        )

    result = live_slice.live_slice_status(layout)
    return _output(step="status", result=result), (0 if result.get("healthy") else 1)


def main(argv: Iterable[str] | None = None) -> int:
    try:
        args = parse_args(argv)
        payload, return_code = _run_step(args, _layout_from_args(args))
        print(json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True))
        return return_code
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"DEV live flow error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
