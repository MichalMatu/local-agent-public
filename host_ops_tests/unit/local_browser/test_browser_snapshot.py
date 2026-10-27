from __future__ import annotations

import importlib
import json

import pytest

from local_agent.host_ops.capabilities.local.browser import (
    BrowserCdpSnapshotter,
    BrowserPageSnapshot,
    BrowserSnapshotError,
)
from local_agent.host_ops.core.execution import ProcessResult, ProcessState

snapshot = importlib.import_module("local_agent.host_ops.capabilities.local.browser.snapshot")


def _evidence() -> BrowserPageSnapshot:
    return BrowserPageSnapshot(
        endpoint="http://127.0.0.1:9222",
        target_id="page-1",
        target_type="page",
        title="Example",
        url="https://example.test/path",
        frame_count=2,
        main_frame_url="https://example.test/path",
        main_frame_mime_type="text/html",
        document_node_name="#document",
        document_child_count=2,
    )


def _result(**kwargs) -> ProcessResult:
    defaults = dict(
        state=ProcessState.COMPLETED, exit_code=0, stdout="", stderr="", duration_seconds=0.1
    )
    defaults.update(kwargs)
    return ProcessResult(**defaults)


def test_snapshot_validates_endpoint_target_and_timeout_before_backend() -> None:
    calls = []

    def backend(endpoint: str, target_id: str, timeout_seconds: float):
        calls.append((endpoint, target_id, timeout_seconds))
        return _evidence()

    result = BrowserCdpSnapshotter(backend).snapshot(
        " http://127.0.0.1:9222/ ", " page-1 ", timeout_seconds=4.5
    )
    assert result == _evidence()
    assert calls == [("http://127.0.0.1:9222", "page-1", 4.5)]


@pytest.mark.parametrize("target_id", ["", "has space", "x" * 257, "line\nbreak"])
def test_snapshot_rejects_invalid_target_id(target_id: str) -> None:
    with pytest.raises(ValueError, match="target id"):
        BrowserCdpSnapshotter(lambda *_args: _evidence()).snapshot(
            "http://127.0.0.1:9222", target_id
        )


@pytest.mark.parametrize("timeout_seconds", [0.5, 0, -1, 120.1])
def test_snapshot_rejects_invalid_timeout(timeout_seconds: float) -> None:
    with pytest.raises(ValueError, match="timeout_seconds"):
        BrowserCdpSnapshotter(lambda *_args: _evidence()).snapshot(
            "http://127.0.0.1:9222", "page-1", timeout_seconds=timeout_seconds
        )


def test_snapshot_helper_keeps_endpoint_and_target_out_of_argv(monkeypatch) -> None:
    calls = []

    class FakeRunner:
        def run(self, argv, *, cwd=None, env_overrides=None, limits=None):
            calls.append((list(argv), dict(env_overrides or {}), limits))
            return _result(stdout=json.dumps(_evidence().as_dict()))

    monkeypatch.setattr(snapshot, "ProcessRunner", FakeRunner)
    result = BrowserCdpSnapshotter().snapshot("http://127.0.0.1:9222", "page-1", timeout_seconds=7)
    assert result == _evidence()
    argv, env, limits = calls[0]
    assert argv[2] == (
        "local_agent.host_ops.capabilities.local.browser.playwright_snapshot"
    )
    assert "http://127.0.0.1:9222" not in argv
    assert "page-1" not in argv
    assert env["HOST_OPS_BROWSER_ATTACH_ENDPOINT"] == "http://127.0.0.1:9222"
    assert env[snapshot._SNAPSHOT_TARGET_ENV] == "page-1"
    assert limits.timeout_seconds == 7


def test_snapshot_timeout_and_truncation_fail_closed(monkeypatch) -> None:
    class TimeoutRunner:
        def run(self, *args, **kwargs):
            return _result(state=ProcessState.TIMED_OUT, exit_code=None, stdout="private")

    monkeypatch.setattr(snapshot, "ProcessRunner", TimeoutRunner)
    with pytest.raises(BrowserSnapshotError, match="whole-process timeout"):
        BrowserCdpSnapshotter().snapshot("http://127.0.0.1:9222", "page-1")

    class TruncatedRunner:
        def run(self, *args, **kwargs):
            return _result(stdout=json.dumps(_evidence().as_dict()), stdout_truncated=True)

    monkeypatch.setattr(snapshot, "ProcessRunner", TruncatedRunner)
    with pytest.raises(BrowserSnapshotError, match="output exceeded"):
        BrowserCdpSnapshotter().snapshot("http://127.0.0.1:9222", "page-1")


def test_snapshot_rejects_invalid_helper_json(monkeypatch) -> None:
    class FakeRunner:
        def run(self, *args, **kwargs):
            return _result(stdout="not-json")

    monkeypatch.setattr(snapshot, "ProcessRunner", FakeRunner)
    with pytest.raises(BrowserSnapshotError, match="invalid JSON"):
        BrowserCdpSnapshotter().snapshot("http://127.0.0.1:9222", "page-1")


def test_snapshot_rejects_wrong_identity_and_unsanitized_urls() -> None:
    payload = _evidence().as_dict()
    payload["target_id"] = "other"
    with pytest.raises(BrowserSnapshotError, match="wrong target id"):
        snapshot._snapshot_from_payload(
            payload, expected_endpoint="http://127.0.0.1:9222", expected_target_id="page-1"
        )


def test_snapshot_model_contains_metadata_only() -> None:
    payload = _evidence().as_dict()
    assert set(payload) == {
        "endpoint",
        "target_id",
        "target_type",
        "title",
        "url",
        "frame_count",
        "main_frame_url",
        "main_frame_mime_type",
        "document_node_name",
        "document_child_count",
    }
    assert "html" not in payload
    assert "cookies" not in payload


def test_snapshot_helper_failure_paths_fail_closed_and_redact(monkeypatch) -> None:
    class StructuredFailureRunner:
        def run(self, *args, **kwargs):
            return _result(
                exit_code=7,
                stdout=json.dumps(
                    {"error": "attach failed at https://example.test/path?token=secret"}
                ),
            )

    monkeypatch.setattr(snapshot, "ProcessRunner", StructuredFailureRunner)
    with pytest.raises(BrowserSnapshotError) as exc_info:
        BrowserCdpSnapshotter().snapshot("http://127.0.0.1:9222", "page-1")
    assert "secret" not in str(exc_info.value)
    assert "<redacted-url>" in str(exc_info.value)

    class BareFailureRunner:
        def run(self, *args, **kwargs):
            return _result(exit_code=9, stdout="{}")

    monkeypatch.setattr(snapshot, "ProcessRunner", BareFailureRunner)
    with pytest.raises(BrowserSnapshotError, match=r"helper failed: completed exit=9"):
        BrowserCdpSnapshotter().snapshot("http://127.0.0.1:9222", "page-1")


def test_snapshot_helper_rejects_non_object_json_and_wrong_endpoint() -> None:
    with pytest.raises(BrowserSnapshotError, match="non-object JSON"):
        snapshot._parse_helper_json("[]")

    payload = _evidence().as_dict()
    payload["endpoint"] = "http://127.0.0.1:9333"
    with pytest.raises(BrowserSnapshotError, match="wrong endpoint"):
        snapshot._snapshot_from_payload(
            payload, expected_endpoint="http://127.0.0.1:9222", expected_target_id="page-1"
        )


@pytest.mark.parametrize(
    ("key", "value", "message"),
    [
        ("target_type", "worker", "non-page target"),
        ("title", "x" * 513, "invalid title"),
        ("url", "https://example.test/path?token=secret", "unsanitized url"),
        ("frame_count", True, "invalid frame_count"),
        ("main_frame_url", None, "invalid main_frame_url"),
        ("main_frame_mime_type", object(), "invalid main_frame_mime_type"),
        ("document_node_name", "", "invalid document_node_name"),
        ("document_child_count", True, "invalid document_child_count"),
    ],
)
def test_snapshot_payload_bounds_fail_closed(key: str, value: object, message: str) -> None:
    payload = _evidence().as_dict()
    payload[key] = value
    with pytest.raises(BrowserSnapshotError, match=message):
        snapshot._snapshot_from_payload(
            payload, expected_endpoint="http://127.0.0.1:9222", expected_target_id="page-1"
        )
