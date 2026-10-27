"""Shared CLI resolution of configured host targets."""

from __future__ import annotations

from local_agent.host_ops.capabilities.remote.ssh import UnknownHostError, resolve_host
from local_agent.host_ops.core.config import ConfigError, HostTarget, load_config


class HostTargetResolutionError(ValueError):
    """Raised when a CLI host alias cannot be resolved safely."""


def load_host_target(alias: str) -> HostTarget:
    try:
        return resolve_host(load_config(), alias)
    except (ConfigError, UnknownHostError) as exc:
        raise HostTargetResolutionError(str(exc)) from exc
