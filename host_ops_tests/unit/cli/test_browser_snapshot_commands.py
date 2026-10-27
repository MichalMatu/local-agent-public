from __future__ import annotations

import importlib
import json

from local_agent.host_ops.capabilities.local.browser import BrowserPageSnapshot, BrowserSnapshotError

cli_main = importlib.import_module("local_agent.host_ops.cli.main")
browser_cmd = importlib.import_module("local_agent.host_ops.cli.commands.browser")


def _snapshot() -> BrowserPageSnapshot:
    return BrowserPageSnapshot(
        endpoint="http://127.0.0.1:9222",
        target_id="page-1",
        target_type="page",
        title="Example",
        url="https://example.test/",
        frame_count=1,
        main_frame_url="https://example.test/",
        main_frame_mime_type="text/html",
        document_node_name="#document",
        document_child_count=2,
    )


def test_snapshot_parser_exposes_endpoint_target_timeout_and_json() -> None:
    args = cli_main.build_parser().parse_args(
        [
            "browser",
            "attach",
            "snapshot",
            "--endpoint",
            "http://127.0.0.1:9222",
            "--target-id",
            "page-1",
            "--timeout",
            "9",
            "--json",
        ]
    )
    assert args.browser_command == "attach"
    assert args.browser_attach_command == "snapshot"
    assert args.endpoint == "http://127.0.0.1:9222"
    assert args.target_id == "page-1"
    assert args.timeout_seconds == 9
    assert args.as_json is True


def test_run_snapshot_renders_json(monkeypatch, capsys) -> None:
    class FakeSnapshotter:
        def snapshot(self, endpoint, target_id, *, timeout_seconds):
            return _snapshot()

    monkeypatch.setattr(browser_cmd, "BrowserCdpSnapshotter", FakeSnapshotter)
    assert (
        browser_cmd.run_attach_snapshot(
            "http://127.0.0.1:9222", "page-1", timeout_seconds=9, as_json=True
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["target_id"] == "page-1"


def test_run_snapshot_reports_failure(monkeypatch, capsys) -> None:
    class FakeSnapshotter:
        def snapshot(self, endpoint, target_id, *, timeout_seconds):
            raise BrowserSnapshotError("not found")

    monkeypatch.setattr(browser_cmd, "BrowserCdpSnapshotter", FakeSnapshotter)
    assert (
        browser_cmd.run_attach_snapshot(
            "http://127.0.0.1:9222", "page-1", timeout_seconds=9, as_json=True
        )
        == 1
    )
    assert "not found" in json.loads(capsys.readouterr().err)["error"]


def test_main_dispatches_snapshot(monkeypatch) -> None:
    calls = []

    def run(endpoint, target_id, *, timeout_seconds, as_json):
        calls.append((endpoint, target_id, timeout_seconds, as_json))
        return 17

    monkeypatch.setattr(browser_cmd, "run_attach_snapshot", run)
    assert (
        cli_main.main(
            [
                "browser",
                "attach",
                "snapshot",
                "--endpoint",
                "http://127.0.0.1:9222",
                "--target-id",
                "page-1",
                "--json",
            ]
        )
        == 17
    )
    assert calls == [("http://127.0.0.1:9222", "page-1", 10.0, True)]
