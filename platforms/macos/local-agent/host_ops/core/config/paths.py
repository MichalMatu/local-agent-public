"""Resolve machine-local host-ops configuration paths."""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path


def config_directory(
    *,
    env: Mapping[str, str] | None = None,
    home: Path | None = None,
) -> Path:
    environment = os.environ if env is None else env
    xdg_home = environment.get("XDG_CONFIG_HOME")
    if xdg_home:
        return Path(xdg_home).expanduser() / "host-ops"
    return (home or Path.home()) / ".config" / "host-ops"


def config_file_path(
    *,
    env: Mapping[str, str] | None = None,
    home: Path | None = None,
) -> Path:
    environment = os.environ if env is None else env
    override = environment.get("HOST_OPS_CONFIG")
    if override:
        return Path(override).expanduser()
    return config_directory(env=environment, home=home) / "config.toml"
