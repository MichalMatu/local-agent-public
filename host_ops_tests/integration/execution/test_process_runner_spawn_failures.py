from __future__ import annotations

import sys
from pathlib import Path

from local_agent.host_ops.core.execution import ProcessRunner, ProcessState


def test_process_runner_normalizes_invalid_cwd_value() -> None:
    invalid_cwd = Path("bad\x00cwd")

    result = ProcessRunner().run((sys.executable, "-c", "pass"), cwd=invalid_cwd)

    assert result.state is ProcessState.SPAWN_FAILED
    assert result.exit_code is None
    assert result.error is not None
    assert "ValueError" in result.error
