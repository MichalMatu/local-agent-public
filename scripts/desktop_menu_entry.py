"""Standalone, optional desktop UI entry for macOS .app packaging.

Not a daemon or controller and not imported by any headless service.
"""
from local_agent.desktop.tray import main


if __name__ == "__main__":
    raise SystemExit(main())
