"""Reject user-controlled path symlinks without rejecting macOS system temp aliases."""

from __future__ import annotations

import sys
from pathlib import Path

_MACOS_SYSTEM_ALIASES = {
    Path("/var"): Path("/private/var"),
    Path("/tmp"): Path("/private/tmp"),
}


def has_untrusted_symlink(path: Path) -> bool:
    """Allow only native macOS /var and /tmp ancestor aliases, never leaf links.

    Callers still independently require absolute paths, isolated staging roots,
    regular files and destination containment. The exception is deliberately
    limited to two immutable system path locations, not arbitrary ancestors.
    """
    candidate = Path(path)
    for component in (candidate, *candidate.parents):
        if not component.is_symlink():
            continue
        if (
            sys.platform == "darwin"
            and component != candidate
            and component in _MACOS_SYSTEM_ALIASES
            and component.resolve() == _MACOS_SYSTEM_ALIASES[component]
        ):
            continue
        return True
    return False
