from __future__ import annotations

import importlib
import json

from local_agent.host_ops.capabilities.local.browser import (
    BrowserExtensionScriptEvidence,
    BrowserReadinessError,
    BrowserReadinessInspection,
    BrowserSelectorCount,
)

cli_main = importlib.import_module("local_agent.host_ops.cli.main")
browser_cmd = importlib.import_module("local_agent.host_ops.cli.commands.browser")

DIGEST = "a" * 64


def _result() -> BrowserReadinessInspection:
    return BrowserReadinessInspection(
        endpoint="http://127.0.0.1:9222",
        target_id="page-1",
        target_type="page",
        title="Example",
        url="https://example.test/path",
        selectors=(BrowserSelectorCount("#prompt-textarea", 1),),
        extension_scripts=(BrowserExtensionScriptEvidence("content.js", "matched"),),
        dom_ready=True,
        content_script_state="ready",
        worker_state="running",
        diagnosis="ready",
    )


def test_readiness_parser_exposes_exact_target_selectors_and_fingerprints() -> None:
    args = cli_main.build_parser().parse_args(
        [
            "browser",
            "attach",
            "readiness",
            "--endpoint",
            "http://127.0.0.1:9222",
            "--target-id",
            "page-1",
            "--selector",
            "#prompt-textarea",
            "--script-fingerprint",
            f"content.js={DIGEST}",
            "--timeout",
            "8",
            "--json",
        ]
    )
    assert args.browser_command == "attach"
    assert args.browser_attach_command == "readiness"
    assert args.endpoint == "http://127.0.0.1:9222"
    assert args.target_id == "page-1"
    assert args.selectors == ["#prompt-textarea"]
    assert args.script_fingerprints == [f"content.js={DIGEST}"]
    assert args.timeout_seconds == 8
    assert args.as_json is True


def test_run_readiness_renders_json_plain_and_failure(monkeypatch, capsys) -> None:
    class FakeInspector:
        def inspect(self, endpoint, target_id, selectors, fingerprints, *, timeout_seconds):
            return _result()

    monkeypatch.setattr(browser_cmd, "BrowserCdpReadinessInspector", FakeInspector)
    assert (
        browser_cmd.run_attach_readiness(
            "http://127.0.0.1:9222",
            "page-1",
            ["#prompt-textarea"],
            [f"content.js={DIGEST}"],
            timeout_seconds=8,
            as_json=True,
        )
        == 0
    )
    output = json.loads(capsys.readouterr().out)
    assert output["diagnosis"] == "ready"
    assert output["extension_scripts"] == [{"name": "content.js", "status": "matched"}]

    assert (
        browser_cmd.run_attach_readiness(
            "http://127.0.0.1:9222",
            "page-1",
            ["#prompt-textarea"],
            [f"content.js={DIGEST}"],
            timeout_seconds=8,
            as_json=False,
        )
        == 0
    )
    plain = capsys.readouterr().out
    assert "diagnosis=ready" in plain
    assert "extension_script='content.js' status=matched" in plain

    class FailedInspector:
        def inspect(self, endpoint, target_id, selectors, fingerprints, *, timeout_seconds):
            raise BrowserReadinessError("readiness evidence failed")

    monkeypatch.setattr(browser_cmd, "BrowserCdpReadinessInspector", FailedInspector)
    assert (
        browser_cmd.run_attach_readiness(
            "http://127.0.0.1:9222",
            "page-1",
            ["#prompt-textarea"],
            [f"content.js={DIGEST}"],
            timeout_seconds=8,
            as_json=True,
        )
        == 1
    )
    assert "readiness evidence failed" in json.loads(capsys.readouterr().err)["error"]


def test_main_dispatches_readiness(monkeypatch) -> None:
    calls = []

    def run(endpoint, target_id, selectors, fingerprints, *, timeout_seconds, as_json):
        calls.append((endpoint, target_id, selectors, fingerprints, timeout_seconds, as_json))
        return 23

    monkeypatch.setattr(browser_cmd, "run_attach_readiness", run)
    assert (
        cli_main.main(
            [
                "browser",
                "attach",
                "readiness",
                "--endpoint",
                "http://127.0.0.1:9222",
                "--target-id",
                "page-1",
                "--selector",
                "#prompt-textarea",
                "--script-fingerprint",
                f"content.js={DIGEST}",
                "--json",
            ]
        )
        == 23
    )
    assert calls == [
        (
            "http://127.0.0.1:9222",
            "page-1",
            ["#prompt-textarea"],
            [f"content.js={DIGEST}"],
            10.0,
            True,
        )
    ]
