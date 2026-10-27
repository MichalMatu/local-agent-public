"""Compose deterministic environment diagnostics."""

from __future__ import annotations

import shutil
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from local_agent.host_ops.core.config import config_file_path

from .checks import CommandRequirement, DiagnosticCheck, check_command, check_config, check_python


@dataclass(frozen=True, slots=True)
class DoctorReport:
    checks: tuple[DiagnosticCheck, ...]

    @property
    def ok(self) -> bool:
        return all(check.ok for check in self.checks)

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "checks": [check.as_dict() for check in self.checks],
        }


def run_doctor(
    *,
    command_requirements: Sequence[CommandRequirement] = (),
    config_path: Path | None = None,
    command_resolver: Callable[[str], str | None] = shutil.which,
) -> DoctorReport:
    selected_config = config_path or config_file_path()
    checks: list[DiagnosticCheck] = [check_python(), check_config(selected_config)]
    checks.extend(
        check_command(requirement, resolver=command_resolver)
        for requirement in command_requirements
    )
    return DoctorReport(tuple(checks))
