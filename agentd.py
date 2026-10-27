#!/usr/bin/env python3
"""Operational launcher for the registry-backed Local Agent daemon."""

from local_agent.daemon.launcher import run

if __name__ == "__main__":
    raise SystemExit(run())
