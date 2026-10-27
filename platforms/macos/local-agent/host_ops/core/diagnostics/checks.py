"""Reusable diagnostic check contracts."""

from __future__ import annotations

import shutil
import sys
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from local_agent.host_ops.core.config import ConfigError, load_config


class CheckStatus(StrEnum):
    PASS = "pass"  # nosec B105 - diagnostic status token, not a credential
    WARN = "warn"
    FAIL = "fail"


@dataclass(frozen=True, slots=True)
class DiagnosticCheck:
    name: str
    status: CheckStatus
    summary: str

    @property
    def ok(self) -> bool:
        return self.status is not CheckStatus.FAIL

    def as_dict(self) -> dict[str, str]:
        return {
            "name": self.name,
            "status": self.status.value,
            "summary": self.summary,
        }


@dataclass(frozen=True, slots=True)
class CommandRequirement:
    name: str
    purpose: str
    required: bool = False


def check_python(minimum: tuple[int, int] = (3, 12)) -> DiagnosticCheck:
    actual = sys.version_info[:2]
    if actual >= minimum:
        return DiagnosticCheck(
            "python",
            CheckStatus.PASS,
            f"Python {actual[0]}.{actual[1]} satisfies >= {minimum[0]}.{minimum[1]}",
        )
    return DiagnosticCheck(
        "python",
        CheckStatus.FAIL,
        f"Python {actual[0]}.{actual[1]} is below required {minimum[0]}.{minimum[1]}",
    )


def check_command(
    requirement: CommandRequirement,
    *,
    resolver: Callable[[str], str | None] = shutil.which,
) -> DiagnosticCheck:
    path = resolver(requirement.name)
    if path:
        return DiagnosticCheck(
            f"command:{requirement.name}",
            CheckStatus.PASS,
            f"{requirement.name} available at {path}",
        )

    status = CheckStatus.FAIL if requirement.required else CheckStatus.WARN
    return DiagnosticCheck(
        f"command:{requirement.name}",
        status,
        f"{requirement.name} not found; {requirement.purpose}",
    )


def check_config(path: Path) -> DiagnosticCheck:
    if not path.exists():
        return DiagnosticCheck(
            "config",
            CheckStatus.WARN,
            f"configuration file not present at {path}; defaults will be used",
        )

    try:
        load_config(path)
    except ConfigError as exc:
        return DiagnosticCheck(
            "config",
            CheckStatus.FAIL,
            f"configuration at {path} is invalid: {exc}",
        )

    return DiagnosticCheck(
        "config",
        CheckStatus.PASS,
        f"configuration file is valid at {path}",
    )
