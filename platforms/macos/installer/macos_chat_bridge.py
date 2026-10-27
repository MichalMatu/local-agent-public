#!/usr/bin/env python3
"""Install or upgrade only the separate macOS Chat Bridge transport service."""
from __future__ import annotations

import argparse
import platform
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from local_agent.platform.macos_bridge_pairing import (  # noqa: E402
    setup_pairing_service, upgrade_pairing_to_transport,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("pair-only", "enable-transport"))
    parser.add_argument("--repo-root", type=Path, default=ROOT)
    parser.add_argument("--home", type=Path, default=Path.home())
    args = parser.parse_args()
    if platform.system() != "Darwin":
        parser.error("macOS required")
    if args.operation == "enable-transport":
        outcome = upgrade_pairing_to_transport(repo_root=args.repo_root, home=args.home)
    else:
        outcome = setup_pairing_service(repo_root=args.repo_root, home=args.home)
    print(f"Chat Bridge {args.operation}: {outcome} (main daemon unchanged)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
