"""Resolve configured SSH targets by stable host-ops alias."""

from __future__ import annotations

from local_agent.host_ops.core.config import HostOpsConfig, HostTarget


class UnknownHostError(LookupError):
    """Requested host alias is absent from machine-local configuration."""


def resolve_host(config: HostOpsConfig, alias: str) -> HostTarget:
    try:
        return config.hosts[alias]
    except KeyError as exc:
        raise UnknownHostError(f"unknown host alias: {alias}") from exc
