"""Read-only Raspberry Pi Zero 2 W platform admission, not an installer."""
from __future__ import annotations

import json
import platform
import sys
from pathlib import Path


class UnsupportedPi(ValueError):
    """The physical platform has not passed this port's native gate."""


def validate_platform(
    *, system: str, machine: str, model: str, os_id: str
) -> dict[str, str]:
    """Reject accidental execution on Mac/Debian/old Pi Zero (ARMv6).

    Initial supported target is the historically ARM64-tested Pi Zero 2 W.
    The 32-bit armv7l userspace requires its own native acceptance.
    """
    if system != "Linux":
        raise UnsupportedPi("Raspberry Pi service requires Linux")
    if "Raspberry Pi Zero 2 W" not in model:
        raise UnsupportedPi("unsupported Raspberry Pi model")
    if machine != "aarch64":
        raise UnsupportedPi("Pi Zero 2 W requires accepted 64-bit ARM userspace")
    if os_id not in {"debian", "raspbian"}:
        raise UnsupportedPi("untested Raspberry Pi operating system")
    return {"platform": "raspberry-pi-zero-2-w", "arch": machine,
            "os_id": os_id, "model": model.strip()}


def read_host() -> dict[str, str]:
    """Observe only local, public OS identity; no Git or service side effects."""
    try:
        model = Path("/proc/device-tree/model").read_bytes().rstrip(b"\x00").decode("utf-8")
        os_release = Path("/etc/os-release").read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise UnsupportedPi("physical Pi model or OS release unavailable") from exc
    os_id = ""
    for line in os_release.splitlines():
        if line.startswith("ID="):
            os_id = line.partition("=")[2].strip().strip('"').lower()
            break
    return validate_platform(system=platform.system(), machine=platform.machine(),
                             model=model, os_id=os_id)


def probe_json() -> str:
    return json.dumps(read_host(), sort_keys=True)


def main() -> int:
    # A source-only probe can be run from the installed historical Pi task lane.
    try:
        print(probe_json())
    except UnsupportedPi as exc:
        print(f"Pi preflight rejected: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
