"""CLI rendering for local executable inspection."""

from __future__ import annotations

import json
import sys
from collections.abc import Sequence

from local_agent.host_ops.capabilities.local.tools import ToolInspectionError, ToolInspector
from local_agent.host_ops.core.execution import ExecutionLimits


def run_inspect(
    names: Sequence[str],
    *,
    timeout_seconds: float,
    as_json: bool,
) -> int:
    try:
        limits = ExecutionLimits(
            timeout_seconds=timeout_seconds,
            max_stdout_bytes=8192,
            max_stderr_bytes=8192,
        )
        results = ToolInspector().inspect(names, limits=limits)
    except (ToolInspectionError, ValueError) as exc:
        return _error(exc, as_json=as_json)

    payload = [result.as_dict() for result in results]
    if as_json:
        print(json.dumps(payload, sort_keys=True))
        return 0
    for result in results:
        fields = [f"name={result.name}", f"present={str(result.present).lower()}"]
        if result.resolved_path is not None:
            fields.append(f"path={result.resolved_path}")
        if result.version is not None:
            fields.append(f"version={result.version}")
        if result.error is not None:
            fields.append(f"error={result.error}")
        print(" ".join(fields))
    return 0


def _error(exc: Exception, *, as_json: bool) -> int:
    if as_json:
        print(json.dumps({"error": str(exc)}, sort_keys=True), file=sys.stderr)
    else:
        print(f"tool inspection failed: {exc}", file=sys.stderr)
    return 1
