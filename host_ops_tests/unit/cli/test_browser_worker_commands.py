from __future__ import annotations

import importlib
import json

from local_agent.host_ops.capabilities.local.browser import (
    BrowserServiceWorkerRegistration,
    BrowserServiceWorkerVersion,
    BrowserWorkerDiagnostics,
    BrowserWorkerDiagnosticsError,
    BrowserWorkerTarget,
)

cli_main = importlib.import_module("local_agent.host_ops.cli.main")
browser_cmd = importlib.import_module("local_agent.host_ops.cli.commands.browser")


def _result() -> BrowserWorkerDiagnostics:
    return BrowserWorkerDiagnostics(
        endpoint="http://127.0.0.1:9222",
        worker_targets=(
            BrowserWorkerTarget(
                target_id="worker-1",
                target_type="service_worker",
                url="chrome-extension:",
                attached=False,
            ),
        ),
        registrations=(
            BrowserServiceWorkerRegistration(scope_url="chrome-extension:", is_deleted=False),
        ),
        versions=(
            BrowserServiceWorkerVersion(
                target_id="worker-1",
                scope_url="chrome-extension:",
                script_url="chrome-extension:",
                running_status="running",
                lifecycle_status="activated",
                controlled_client_count=1,
                target_attached=False,
                registration_deleted=False,
            ),
        ),
        error_count=0,
    )


def test_workers_parser_exposes_endpoint_timeout_and_json() -> None:
    args = cli_main.build_parser().parse_args(
        [
            "browser",
            "attach",
            "workers",
            "--endpoint",
            "http://127.0.0.1:9222",
            "--timeout",
            "8",
            "--json",
        ]
    )

    assert args.browser_command == "attach"
    assert args.browser_attach_command == "workers"
    assert args.endpoint == "http://127.0.0.1:9222"
    assert args.timeout_seconds == 8
    assert args.as_json is True


def test_run_workers_renders_json_and_failure(monkeypatch, capsys) -> None:
    class FakeDiagnoser:
        def inspect(self, endpoint, *, timeout_seconds):
            return _result()

    monkeypatch.setattr(browser_cmd, "BrowserCdpWorkerDiagnoser", FakeDiagnoser)
    assert (
        browser_cmd.run_attach_workers("http://127.0.0.1:9222", timeout_seconds=8, as_json=True)
        == 0
    )
    output = json.loads(capsys.readouterr().out)
    assert output["versions"][0]["running_status"] == "running"
    assert output["versions"][0]["lifecycle_status"] == "activated"

    class FailedDiagnoser:
        def inspect(self, endpoint, *, timeout_seconds):
            raise BrowserWorkerDiagnosticsError("worker evidence failed")

    monkeypatch.setattr(browser_cmd, "BrowserCdpWorkerDiagnoser", FailedDiagnoser)
    assert (
        browser_cmd.run_attach_workers("http://127.0.0.1:9222", timeout_seconds=8, as_json=True)
        == 1
    )
    assert "worker evidence failed" in json.loads(capsys.readouterr().err)["error"]


def test_main_dispatches_workers_diagnostics(monkeypatch) -> None:
    calls = []

    def run(endpoint, *, timeout_seconds, as_json):
        calls.append((endpoint, timeout_seconds, as_json))
        return 23

    monkeypatch.setattr(browser_cmd, "run_attach_workers", run)
    assert (
        cli_main.main(
            [
                "browser",
                "attach",
                "workers",
                "--endpoint",
                "http://127.0.0.1:9222",
                "--json",
            ]
        )
        == 23
    )
    assert calls == [("http://127.0.0.1:9222", 10.0, True)]
