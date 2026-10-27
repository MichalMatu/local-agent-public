"""Termux-owned control checkout; same immutable Linux/Mac wire protocol."""
from pathlib import Path
from typing import Any

from linux_agent.git_transport import GitTransport


class TermuxGitTransport(GitTransport):
    def __init__(self, config: dict[str, Any], state: Path):
        super().__init__(config, state)
        self.marker = state / 'transport' / 'OWNED_BY_TERMUX_AGENT.json'
        # Separate path prevents accidental reuse of a Debian control clone.
        self.checkout = state / 'transport' / 'termux-agent-control'
