"""Validated configuration models."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

_ALIAS_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_HOST_PATTERN = re.compile(r"^[A-Za-z0-9._:-]+$")
_USER_PATTERN = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]*$")


@dataclass(frozen=True, slots=True)
class HostTarget:
    alias: str
    host: str
    port: int = 22
    user: str | None = None
    identity_file: str | None = None
    known_hosts_file: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.alias, str) or not _ALIAS_PATTERN.fullmatch(self.alias):
            raise ValueError(f"invalid host alias: {self.alias!r}")
        if (
            not isinstance(self.host, str)
            or self.host.startswith("-")
            or not _HOST_PATTERN.fullmatch(self.host)
        ):
            raise ValueError("host must be a literal DNS/IP endpoint without option or URI syntax")
        if (
            isinstance(self.port, bool)
            or not isinstance(self.port, int)
            or not 1 <= self.port <= 65535
        ):
            raise ValueError("port must be an integer in range 1..65535")
        if self.user is not None and (
            not isinstance(self.user, str) or not _USER_PATTERN.fullmatch(self.user)
        ):
            raise ValueError("user contains unsupported characters")
        if self.identity_file is not None:
            if not isinstance(self.identity_file, str):
                raise ValueError("identity_file must be a string path")
            if (
                not self.identity_file
                or self.identity_file.startswith("-")
                or "%" in self.identity_file
                or "$" in self.identity_file
                or any(ord(char) < 32 or ord(char) == 127 for char in self.identity_file)
            ):
                raise ValueError(
                    "identity_file must be a literal non-option path without OpenSSH expansion "
                    "tokens or control characters"
                )
            if not (self.identity_file.startswith("~/") or Path(self.identity_file).is_absolute()):
                raise ValueError("identity_file must be absolute or start with '~/'.")
        if self.known_hosts_file is not None:
            value = self.known_hosts_file
            if (
                not isinstance(value, str)
                or not value
                or not (value.startswith("~/") or Path(value).is_absolute())
                or any(char in value for char in ('%', '$', '"', '\\'))
                or any(ord(char) < 32 or ord(char) == 127 for char in value)
            ):
                raise ValueError("known_hosts_file must be a literal absolute or '~/' file path")


@dataclass(frozen=True, slots=True)
class HostOpsConfig:
    version: int = 1
    hosts: dict[str, HostTarget] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if isinstance(self.version, bool) or not isinstance(self.version, int) or self.version != 1:
            raise ValueError(f"unsupported config version: {self.version}")
        if not isinstance(self.hosts, dict):
            raise ValueError("hosts must be a dictionary")
        for alias, target in self.hosts.items():
            if not isinstance(alias, str) or not isinstance(target, HostTarget):
                raise ValueError("hosts must map string aliases to HostTarget values")
            if alias != target.alias:
                raise ValueError(f"host mapping key {alias!r} does not match target alias")
