from __future__ import annotations

import local_agent.daemon.service as service


def run() -> None:
    """Dispatch only through the registry-backed supervisor execution path."""
    if service.dispatch_multirepo_if_configured():
        return
    registry = service.multirepo_registry_path()
    message = (
        "repository registry is required; refusing legacy single-repository execution "
        f"registry={registry}"
    )
    service.log(message)
    raise RuntimeError(message)
