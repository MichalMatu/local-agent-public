"""CLI rendering and exit-code mapping for bounded process results."""

from __future__ import annotations

import sys

from local_agent.host_ops.core.execution import ProcessResult, ProcessState


def emit_process_output(result: ProcessResult) -> None:
    if result.stdout:
        sys.stdout.write(result.stdout)
    if result.stderr:
        sys.stderr.write(result.stderr)
    if result.error:
        print(result.error, file=sys.stderr)


def process_exit_code(result: ProcessResult) -> int:
    if result.state is ProcessState.TIMED_OUT:
        return 124
    if result.state is ProcessState.LINGERING_DESCENDANTS:
        return 125
    if result.state is ProcessState.SPAWN_FAILED:
        return 127
    if result.exit_code is not None and 0 <= result.exit_code <= 255:
        return result.exit_code
    return 1
