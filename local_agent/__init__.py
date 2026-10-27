"""Stable import path for the relocated macOS Agent package.

All implementation remains in platforms/macos/local-agent.
This thin import-path bridge does not implement a second Core.
"""
from pathlib import Path

__path__ = [str(Path(__file__).resolve().parents[1] / "platforms" / "macos" / "local-agent")]
