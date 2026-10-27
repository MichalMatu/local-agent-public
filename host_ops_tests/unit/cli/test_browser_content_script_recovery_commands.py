from __future__ import annotations

import importlib
import json

from local_agent.host_ops.capabilities.local.browser import (
    BrowserContentScriptRecoveryError,
    BrowserContentScriptRecoveryResult,
    BrowserExtensionScriptEvidence,
    BrowserReadinessInspection,
    BrowserReloadResult,
    BrowserSelectorCount,
)

cli_main = importlib.import_module("local_agent.host_ops.cli.main")
browser_cmd = importlib.import_module("local_agent.host_ops.cli.commands.browser")

ENDPOINT = "http://127.0.0.1:9222"
TARGET = "page-1"
URL = "https://example.test/path"
DIGEST = "a" * 64


def _readiness(diagnosis: str = "ready") -> BrowserReadinessInspection:
    return BrowserReadinessInspection(
        endpoint=ENDPOINT,
        target_id=TARGET,
        target_type="page",
        title="Example",
        url=URL,
        selectors=(BrowserSelectorCount("#ready", 1),),
        extension_scripts=(BrowserExtensionScriptEvidence("content.js", "matched"),),
        dom_ready=True,
        content_script_state="ready",
        worker_state="running",
        diagnosis=diagnosis,
    )


def _result() -> BrowserContentScriptRecoveryResult:
    before = _readiness("content_script_stale")
    after = _readiness("ready")
    reload_result = BrowserReloadResult(
        endpoint=ENDPOINT,
        target_id=TARGET,
        target_type="page",
        expected_url=URL,
        before_url=URL,
        before_title="Before",
        after_url=URL,
        after_title="After",
    )
    return BrowserContentScriptRecoveryResult(
        endpoint=ENDPOINT,
        target_id=TARGET,
        expected_url=URL,
        action="reload",
        outcome="recovered",
        before=before,
        reload=reload_result,
        after=after,
    )


def test_recovery_parser_exposes_guard_selectors_and_fingerprints() -> None:
    args = cli_main.build_parser().parse_args(
        [
            "browser",
            "attach",
            "recover-content-script",
            "--endpoint",
            ENDPOINT,
            "--target-id",
            TARGET,
            "--expect-url",
            URL,
            "--selector",
            "#ready",
            "--script-fingerprint",
            f"content.js={DIGEST}",
            "--timeout",
            "40",
            "--json",
        ]
    )
    assert args.browser_command == "attach"
    assert args.browser_attach_command == "recover-content-script"
    assert args.endpoint == ENDPOINT
    assert args.target_id == TARGET
    assert args.expected_url == URL
    assert args.selectors == ["#ready"]
    assert args.script_fingerprints == [f"content.js={DIGEST}"]
    assert args.timeout_seconds == 40
    assert args.as_json is True


def test_run_recovery_renders_json_plain_and_failure(monkeypatch, capsys) -> None:
    class FakeRecoverer:
        def recover(
            self,
            endpoint,
            target_id,
            expected_url,
            selectors,
            fingerprints,
            *,
            timeout_seconds,
        ):
            return _result()

    monkeypatch.setattr(browser_cmd, "BrowserCdpContentScriptRecoverer", FakeRecoverer)
    assert (
        browser_cmd.run_attach_content_script_recovery(
            ENDPOINT,
            TARGET,
            URL,
            ["#ready"],
            [f"content.js={DIGEST}"],
            timeout_seconds=30,
            as_json=True,
        )
        == 0
    )
    output = json.loads(capsys.readouterr().out)
    assert output["action"] == "reload"
    assert output["outcome"] == "recovered"
    assert output["before"]["diagnosis"] == "content_script_stale"
    assert output["after"]["diagnosis"] == "ready"

    assert (
        browser_cmd.run_attach_content_script_recovery(
            ENDPOINT,
            TARGET,
            URL,
            ["#ready"],
            [f"content.js={DIGEST}"],
            timeout_seconds=30,
            as_json=False,
        )
        == 0
    )
    plain = capsys.readouterr().out
    assert "action=reload" in plain
    assert "outcome=recovered" in plain
    assert "before=content_script_stale" in plain
    assert "after=ready" in plain

    class FailedRecoverer:
        def recover(self, *args, **kwargs):
            raise BrowserContentScriptRecoveryError("bounded recovery failed")

    monkeypatch.setattr(browser_cmd, "BrowserCdpContentScriptRecoverer", FailedRecoverer)
    assert (
        browser_cmd.run_attach_content_script_recovery(
            ENDPOINT,
            TARGET,
            URL,
            ["#ready"],
            [f"content.js={DIGEST}"],
            timeout_seconds=30,
            as_json=True,
        )
        == 1
    )
    assert "bounded recovery failed" in json.loads(capsys.readouterr().err)["error"]


def test_main_dispatches_recovery(monkeypatch) -> None:
    calls = []

    def run(
        endpoint,
        target_id,
        expected_url,
        selectors,
        fingerprints,
        *,
        timeout_seconds,
        as_json,
    ):
        calls.append(
            (
                endpoint,
                target_id,
                expected_url,
                selectors,
                fingerprints,
                timeout_seconds,
                as_json,
            )
        )
        return 23

    monkeypatch.setattr(browser_cmd, "run_attach_content_script_recovery", run)
    assert (
        cli_main.main(
            [
                "browser",
                "attach",
                "recover-content-script",
                "--endpoint",
                ENDPOINT,
                "--target-id",
                TARGET,
                "--expect-url",
                URL,
                "--selector",
                "#ready",
                "--script-fingerprint",
                f"content.js={DIGEST}",
                "--json",
            ]
        )
        == 23
    )
    assert calls == [
        (
            ENDPOINT,
            TARGET,
            URL,
            ["#ready"],
            [f"content.js={DIGEST}"],
            30.0,
            True,
        )
    ]
