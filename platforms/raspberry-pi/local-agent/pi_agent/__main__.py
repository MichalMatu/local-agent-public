"""Pi-specific entrypoint with a fail-closed native gate before Linux runtime."""
from __future__ import annotations

import sys

from pi_agent.preflight import UnsupportedPi, main as probe, read_host


def main() -> int:
    if len(sys.argv) == 2 and sys.argv[1] == "probe":
        return probe()
    try:
        read_host()
    except UnsupportedPi as exc:
        print(f"Pi preflight rejected: {exc}", file=sys.stderr)
        return 2
    # Reuse the independently accepted POSIX/Linux command and Git protocol
    # implementation; never import the legacy Mac supervisor or scheduler.
    from linux_agent.__main__ import main as linux_main

    return linux_main()


if __name__ == "__main__":
    raise SystemExit(main())
