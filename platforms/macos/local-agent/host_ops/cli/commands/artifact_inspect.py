"""CLI for read-only local artifact inspection."""

from __future__ import annotations

import json
import sys
from pathlib import Path

from local_agent.host_ops.capabilities.local.files import ArtifactInspectionError, LocalArtifactInspector
from local_agent.host_ops.core.execution import ExecutionLimits


def run(
    source: str,
    *,
    max_bytes: int,
    timeout_seconds: float,
    as_json: bool,
) -> int:
    try:
        result = LocalArtifactInspector().inspect(
            Path(source),
            max_bytes=max_bytes,
            limits=ExecutionLimits(timeout_seconds=timeout_seconds),
        )
    except (ArtifactInspectionError, ValueError) as exc:
        if as_json:
            print(json.dumps({"error": str(exc)}, sort_keys=True), file=sys.stderr)
        else:
            print(f"artifact inspection failed: {exc}", file=sys.stderr)
        return 1

    if as_json:
        print(json.dumps(result.as_dict(), sort_keys=True))
    else:
        print(
            f"artifact {result.real_path} type={result.file_type} "
            f"sha256={result.sha256} size={result.size_bytes} "
            f"mtime_ns={result.modified_time_ns}"
        )
    return 0
