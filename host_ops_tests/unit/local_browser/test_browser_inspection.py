from __future__ import annotations

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from local_agent.host_ops.capabilities.local.browser import BrowserInspectionError, BrowserInspector
from local_agent.host_ops.core.execution import ProcessResult, ProcessState


class FakeRunner:
    def __init__(self, result: ProcessResult) -> None:
        self.result = result
        self.calls: list[tuple[str, ...]] = []

    def run(self, argv, *, limits=None):
        self.calls.append(tuple(argv))
        return self.result


def _result(
    stdout: str = "",
    *,
    stderr: str = "",
    exit_code: int = 0,
    stdout_truncated: bool = False,
) -> ProcessResult:
    return ProcessResult(
        state=ProcessState.COMPLETED,
        exit_code=exit_code,
        stdout=stdout,
        stderr=stderr,
        duration_seconds=0.01,
        stdout_truncated=stdout_truncated,
    )


def test_inspect_discovers_root_browser_and_redacts_target_url() -> None:
    runner = FakeRunner(
        _result(
            "\n".join(
                [
                    "101 1 /Applications/Google Chrome.app/Contents/MacOS/Google Chrome "
                    "--remote-debugging-port=9222 --remote-debugging-address=127.0.0.1 "
                    "--user-data-dir=/tmp/hostops-browser",
                    "102 101 /Applications/Google Chrome.app/Contents/Frameworks/"
                    "Google Chrome Helper --type=renderer",
                    "201 1 /Applications/Firefox.app/Contents/MacOS/firefox",
                ]
            )
        )
    )
    calls: list[tuple[str, float]] = []

    def fetch(url: str, timeout: float) -> object:
        calls.append((url, timeout))
        if url.endswith("/json/version"):
            return {
                "Browser": "Chrome/153.0",
                "Protocol-Version": "1.3",
                "User-Agent": "test-agent",
                "webSocketDebuggerUrl": "ws://127.0.0.1:9222/devtools/browser/root",
            }
        return [
            {
                "id": "page-1",
                "type": "page",
                "title": "ChatGPT",
                "url": "https://user:password@chatgpt.com:443/c/abc?token=secret#fragment",
                "webSocketDebuggerUrl": "ws://127.0.0.1:9222/devtools/page/page-1",
            }
        ]

    inspection = BrowserInspector(runner, fetch_json=fetch).inspect(timeout_seconds=3.0)

    assert [process.family for process in inspection.processes] == ["chrome", "firefox"]
    chrome = inspection.processes[0]
    assert chrome.remote_debugging_port == 9222
    assert chrome.remote_debugging_address == "127.0.0.1"
    assert chrome.user_data_dir == "/tmp/hostops-browser"
    assert len(inspection.endpoints) == 1
    endpoint = inspection.endpoints[0]
    assert endpoint.reachable is True
    assert endpoint.browser == "Chrome/153.0"
    assert endpoint.targets[0].url == "https://chatgpt.com:443/c/abc"
    assert calls == [
        ("http://127.0.0.1:9222/json/version", 3.0),
        ("http://127.0.0.1:9222/json/list", 3.0),
    ]
    assert runner.calls == [("/bin/ps", "-axo", "pid=,ppid=,command=")]


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://example.com:9222",
        "http://user:pass@127.0.0.1:9222",
        "http://127.0.0.1:9222/json/list",
        "http://127.0.0.1",
        "http://127.0.0.1:0",
        "file:///tmp/devtools",
    ],
)
def test_explicit_endpoint_must_be_loopback_base_url(endpoint: str) -> None:
    inspector = BrowserInspector(FakeRunner(_result()), fetch_json=lambda _url, _timeout: {})

    with pytest.raises(ValueError, match="browser endpoint"):
        inspector.inspect(endpoints=[endpoint])


@settings(max_examples=200, deadline=None, derandomize=True, database=None)
@given(
    scheme=st.sampled_from(("http", "https")),
    host=st.sampled_from(("127.0.0.1", "localhost", "[::1]")),
    port=st.integers(min_value=1, max_value=65535),
)
def test_explicit_loopback_endpoint_property_accepts_full_valid_port_range(
    scheme: str, host: str, port: int
) -> None:
    inspector = BrowserInspector(
        FakeRunner(_result()),
        fetch_json=lambda url, _timeout: {} if url.endswith("/json/version") else [],
    )
    inspection = inspector.inspect(endpoints=[f"{scheme}://{host}:{port}/"])
    assert len(inspection.endpoints) == 1
    assert inspection.endpoints[0].endpoint == f"{scheme}://{host}:{port}"
    assert inspection.endpoints[0].reachable is True


def test_non_loopback_discovered_endpoint_is_not_probed() -> None:
    runner = FakeRunner(
        _result(
            "301 1 /usr/bin/google-chrome --remote-debugging-port=9222 "
            "--remote-debugging-address=0.0.0.0"
        )
    )
    fetch_calls: list[str] = []

    def fetch(url: str, _timeout: float) -> object:
        fetch_calls.append(url)
        return {}

    inspection = BrowserInspector(runner, fetch_json=fetch).inspect()

    assert inspection.endpoints == ()
    assert fetch_calls == []
    assert inspection.warnings == ("pid 301: non-loopback remote debugging address was not probed",)


def test_dynamic_port_and_pipe_report_bounded_warnings() -> None:
    runner = FakeRunner(
        _result(
            "\n".join(
                [
                    "401 1 /usr/bin/google-chrome --remote-debugging-port=0",
                    "402 1 /usr/bin/chromium --remote-debugging-pipe",
                ]
            )
        )
    )

    inspection = BrowserInspector(runner, fetch_json=lambda _url, _timeout: {}).inspect()

    assert inspection.endpoints == ()
    assert inspection.warnings == (
        "pid 401: dynamic remote debugging port cannot be inferred from ps",
        "pid 402: remote debugging uses pipe and is not HTTP-attachable",
    )


def test_endpoint_failure_is_structured_evidence() -> None:
    runner = FakeRunner(_result())

    def fail(_url: str, _timeout: float) -> object:
        raise ValueError("connection refused")

    inspection = BrowserInspector(runner, fetch_json=fail).inspect(
        endpoints=["http://127.0.0.1:9222"]
    )

    assert len(inspection.endpoints) == 1
    endpoint = inspection.endpoints[0]
    assert endpoint.reachable is False
    assert endpoint.error == "connection refused"
    assert endpoint.targets == ()


def test_truncated_process_listing_fails_closed() -> None:
    inspector = BrowserInspector(
        FakeRunner(_result("101 1 /usr/bin/google-chrome", stdout_truncated=True)),
        fetch_json=lambda _url, _timeout: {},
    )

    with pytest.raises(BrowserInspectionError, match="truncated"):
        inspector.inspect()
