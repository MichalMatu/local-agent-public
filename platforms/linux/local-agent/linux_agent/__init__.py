"""Debian service/Host Ops adapters; Linux runtime is in linux/core."""

from pathlib import Path

# Resolve only a sibling package from the exact same source checkout.
_core = Path(__file__).resolve().parents[2] / "core" / "linux_agent"
if _core.is_dir() and not _core.is_symlink():
    __path__.append(str(_core))
