"""Load validated TOML configuration without accepting credential fields."""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any

from .models import HostOpsConfig, HostTarget
from .paths import config_file_path

_ALLOWED_TOP_LEVEL_KEYS = {"version", "hosts"}
_ALLOWED_HOST_KEYS = {"host", "port", "user", "identity_file", "known_hosts_file"}
_CREDENTIAL_LIKE_KEYS = {
    "api_key",
    "apikey",
    "bearer",
    "cookie",
    "credential",
    "credentials",
    "password",
    "passwd",
    "private_key",
    "secret",
    "token",
}


class ConfigError(ValueError):
    """Base error for invalid host-ops configuration."""


class ConfigSecurityError(ConfigError):
    """Configuration attempted to contain credential material."""


def load_config(path: Path | None = None) -> HostOpsConfig:
    selected_path = path or config_file_path()
    if not selected_path.exists():
        return HostOpsConfig()

    try:
        with selected_path.open("rb") as handle:
            raw = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError(f"could not load {selected_path}: {exc}") from exc

    return parse_config(raw)


def parse_config(raw: dict[str, Any]) -> HostOpsConfig:
    unknown_top_level = set(raw) - _ALLOWED_TOP_LEVEL_KEYS
    if unknown_top_level:
        names = ", ".join(sorted(unknown_top_level))
        raise ConfigError(f"unknown top-level configuration key(s): {names}")

    version = raw.get("version", 1)
    if isinstance(version, bool) or not isinstance(version, int):
        raise ConfigError("version must be an integer")

    hosts_raw = raw.get("hosts", {})
    if not isinstance(hosts_raw, dict):
        raise ConfigError("hosts must be a table")

    hosts: dict[str, HostTarget] = {}
    for alias, value in hosts_raw.items():
        if not isinstance(alias, str) or not isinstance(value, dict):
            raise ConfigError("each host entry must be a named table")
        hosts[alias] = _parse_host(alias, value)

    try:
        return HostOpsConfig(version=version, hosts=hosts)
    except ValueError as exc:
        raise ConfigError(str(exc)) from exc


def _parse_host(alias: str, raw: dict[str, Any]) -> HostTarget:
    lowered_keys = {str(key).lower() for key in raw}
    credential_keys = lowered_keys & _CREDENTIAL_LIKE_KEYS
    if credential_keys:
        names = ", ".join(sorted(credential_keys))
        raise ConfigSecurityError(
            f"host {alias!r} contains credential-like field(s): {names}; "
            "use OS/client credential storage instead"
        )

    unknown = set(raw) - _ALLOWED_HOST_KEYS
    if unknown:
        names = ", ".join(sorted(str(key) for key in unknown))
        raise ConfigError(f"host {alias!r} contains unknown field(s): {names}")

    host = raw.get("host")
    port = raw.get("port", 22)
    user = raw.get("user")
    identity_file = raw.get("identity_file")
    if not isinstance(host, str):
        raise ConfigError(f"host {alias!r} requires string field 'host'")
    if isinstance(port, bool) or not isinstance(port, int):
        raise ConfigError(f"host {alias!r} field 'port' must be an integer")
    if user is not None and not isinstance(user, str):
        raise ConfigError(f"host {alias!r} field 'user' must be a string")
    if identity_file is not None and not isinstance(identity_file, str):
        raise ConfigError(f"host {alias!r} field 'identity_file' must be a string")

    try:
        return HostTarget(
            alias=alias,
            host=host,
            port=port,
            user=user,
            identity_file=identity_file,
            known_hosts_file=raw.get("known_hosts_file"),
        )
    except ValueError as exc:
        raise ConfigError(f"host {alias!r}: {exc}") from exc
