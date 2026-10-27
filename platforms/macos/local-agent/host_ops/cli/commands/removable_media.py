"""CLI rendering for explicit removable-media artifact deployment."""

from __future__ import annotations

import json
import sys
from pathlib import Path

from local_agent.host_ops.core.execution import ExecutionLimits
from local_agent.host_ops.workflows.removable_media import (
    MacOSRemovableMediaDeployer,
    RemovableMediaDeploymentError,
)


def run_deploy(
    volume_identifier: str,
    source: str,
    *,
    destination_name: str | None,
    replace: bool,
    eject: bool,
    timeout_seconds: float,
    as_json: bool,
) -> int:
    try:
        result = MacOSRemovableMediaDeployer().deploy(
            Path(source),
            volume_identifier,
            destination_name=destination_name,
            replace=replace,
            eject=eject,
            limits=ExecutionLimits(timeout_seconds=timeout_seconds),
        )
    except (RemovableMediaDeploymentError, ValueError) as exc:
        if isinstance(exc, RemovableMediaDeploymentError):
            payload = exc.as_dict()
        else:
            payload = {"error": str(exc), "stage": "input"}
        if as_json:
            print(json.dumps(payload, sort_keys=True), file=sys.stderr)
        else:
            print(f"removable media deployment failed: {payload['error']}", file=sys.stderr)
        return 1

    if as_json:
        print(json.dumps(result.as_dict(), sort_keys=True))
    else:
        print(
            f"deployed {result.deployment.destination} "
            f"sha256={result.deployment.sha256} ejected={result.ejected}"
        )
    return 0
