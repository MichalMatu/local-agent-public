from __future__ import annotations

from local_agent.host_ops.capabilities.local.browser import BrowserInspector
from local_agent.host_ops.core.execution import ProcessResult, ProcessState


class FakeRunner:
    def run(self, _argv, *, limits=None):
        del limits
        return ProcessResult(
            state=ProcessState.COMPLETED,
            exit_code=0,
            stdout="\n".join(
                [
                    "501 1 /Users/example/.local/share/local-agent/chrome-for-testing/153.0.8010.36/"
                    "chrome-mac-arm64/Google Chrome for Testing.app/Contents/MacOS/"
                    "Google Chrome for Testing --user-data-dir=/tmp/hostops-cft "
                    "--remote-debugging-address=127.0.0.1 --remote-debugging-port=0",
                    "502 501 /Users/example/.local/share/local-agent/"
                    "chrome-for-testing/153.0.8010.36/chrome-mac-arm64/"
                    "Google Chrome for Testing.app/Contents/Frameworks/"
                    "Google Chrome for Testing Framework.framework/Versions/153.0.8010.36/Helpers/"
                    "Google Chrome for Testing Helper.app/Contents/MacOS/"
                    "Google Chrome for Testing Helper --type=renderer",
                ]
            ),
            stderr="",
            duration_seconds=0.01,
        )


def test_inspect_recognizes_google_chrome_for_testing_root_process() -> None:
    inspection = BrowserInspector(FakeRunner(), fetch_json=lambda _url, _timeout: {}).inspect()

    assert len(inspection.processes) == 1
    process = inspection.processes[0]
    assert process.family == "chrome"
    assert process.pid == 501
    assert process.parent_pid == 1
    assert process.user_data_dir == "/tmp/hostops-cft"
    assert process.remote_debugging_address == "127.0.0.1"
    assert process.remote_debugging_port == 0
    assert process.remote_debugging_pipe is False
    assert inspection.endpoints == ()
    assert inspection.warnings == (
        "pid 501: dynamic remote debugging port cannot be inferred from ps",
    )
