#!/usr/bin/env python3
"""Compatibility entrypoint for the platform-owned macOS installer module."""
from __future__ import annotations

import importlib
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
_impl = importlib.import_module("platforms.macos.installer.macos_release_quiescence_rehearsal")
if __name__ == "__main__":
    if hasattr(_impl, "main"):
        raise SystemExit(_impl.main())
else:
    sys.modules[__name__] = _impl
