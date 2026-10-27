"""SSH connectivity and remote-account identity evidence."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from local_agent.host_ops.core.execution import ProcessResult


@dataclass(frozen=True, slots=True)
class SshCheckResult:
    process: ProcessResult
    expected_user: str | None
    remote_user: str | None

    @property
    def identity_matches(self) -> bool:
        if not self.process.ok or self.remote_user is None:
            return False
        return self.expected_user is None or self.remote_user == self.expected_user

    @property
    def ok(self) -> bool:
        return self.process.ok and self.identity_matches

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "identity_matches": self.identity_matches,
            "expected_user": self.expected_user,
            "remote_user": self.remote_user,
            "process": self.process.as_dict(),
        }
