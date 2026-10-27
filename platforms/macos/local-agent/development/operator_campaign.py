"""One-shot DEV adapter from a durable operator request to the accepted MVP campaign."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import local_agent.daemon.service as agentd
from local_agent.supervisor.macos_release_admission import macos_task_admission

from local_agent.conversation.operator_contract import (
    build_operator_result,
    load_operator_request,
    request_to_mvp_spec,
)
from local_agent.conversation.operator_queue import persist_spooled_result
from local_agent.development.lab import build_dev_lab_layout
from local_agent.development.mvp_flow import (
    DEFAULT_LOGIN_TIMEOUT_SECONDS,
    DEFAULT_RESULT_TIMEOUT_SECONDS,
    run_mvp_campaign,
)


def run_operator_campaign(
    *,
    request_path: Path,
    result_path: Path,
    home: Path,
    root: Path,
    checkout: Path,
    production_checkout: Path,
    login_timeout_seconds: int = DEFAULT_LOGIN_TIMEOUT_SECONDS,
    result_timeout_seconds: int = DEFAULT_RESULT_TIMEOUT_SECONDS,
) -> dict:
    """Guard the browser-capable child across its entire task effects."""
    with macos_task_admission(agentd.STATE_DIR):
        return _run_operator_campaign_admitted(
            request_path=request_path,
            result_path=result_path,
            home=home,
            root=root,
            checkout=checkout,
            production_checkout=production_checkout,
            login_timeout_seconds=login_timeout_seconds,
            result_timeout_seconds=result_timeout_seconds,
        )


def _run_operator_campaign_admitted(
    *,
    request_path: Path,
    result_path: Path,
    home: Path,
    root: Path,
    checkout: Path,
    production_checkout: Path,
    login_timeout_seconds: int = DEFAULT_LOGIN_TIMEOUT_SECONDS,
    result_timeout_seconds: int = DEFAULT_RESULT_TIMEOUT_SECONDS,
) -> dict:
    request = load_operator_request(request_path)
    layout = build_dev_lab_layout(
        home=home,
        root=root,
        checkout=checkout,
        production_checkout=production_checkout,
    )
    campaign = run_mvp_campaign(
        layout,
        request_to_mvp_spec(request),
        login_timeout_seconds=login_timeout_seconds,
        result_timeout_seconds=result_timeout_seconds,
    )
    result = build_operator_result(request, campaign)
    return persist_spooled_result(result_path, request, result)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Process one bounded Conversation Fabric operator request through the accepted DEV MVP."
        )
    )
    parser.add_argument("command", choices=("run",))
    parser.add_argument("--request", type=Path, required=True)
    parser.add_argument("--result", type=Path, required=True)
    parser.add_argument("--home", type=Path, required=True)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--checkout", type=Path, required=True)
    parser.add_argument("--production-checkout", type=Path, required=True)
    parser.add_argument(
        "--login-timeout-seconds",
        type=int,
        default=DEFAULT_LOGIN_TIMEOUT_SECONDS,
    )
    parser.add_argument(
        "--result-timeout-seconds",
        type=int,
        default=DEFAULT_RESULT_TIMEOUT_SECONDS,
    )
    return parser


def main() -> int:
    args = _parser().parse_args()
    try:
        result = run_operator_campaign(
            request_path=args.request,
            result_path=args.result,
            home=args.home,
            root=args.root,
            checkout=args.checkout,
            production_checkout=args.production_checkout,
            login_timeout_seconds=args.login_timeout_seconds,
            result_timeout_seconds=args.result_timeout_seconds,
        )
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"Conversation Fabric operator request failed: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
