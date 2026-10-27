"""Second, external-only Debian installed service upgrade.

v1 is committed and immutable. Never reuse or overwrite its journal. This
controller verifies the v1 accepted candidate as its baseline and journals
the second operation independently. Service controls remain external-only.
"""
from __future__ import annotations

import hashlib
import sys
from pathlib import Path
from typing import Any

from linux_agent.release_controller import ServiceSwitchError
from linux_agent.service_plan import ReleasePair
from linux_agent.service_upgrade import (
    PersistentServiceUpgrade, _read_private_json,
)

_V1_KEYS = {
    "version", "unit", "baseline_sha", "candidate_sha",
    "baseline_unit_sha256", "candidate_unit_sha256", "phase",
}


class SecondPersistentServiceUpgrade(PersistentServiceUpgrade):
    """One new baseline -> candidate transaction, retaining committed v1."""

    def __init__(self, pair: ReleasePair):
        super().__init__(pair)
        self.journal = pair.state / "linux-service-upgrade-v2.json"
        self.lock = pair.state / "linux-service-upgrade-v2.lock"

    def _original_receipts(self) -> None:
        """Require the previous committed v1 upgrade and original provenance."""
        prior = _read_private_json(self.pair.state / "linux-service-upgrade-v1.json")
        expected_digest = hashlib.sha256(self.old_unit.encode("utf-8")).hexdigest()
        if (
            set(prior) != _V1_KEYS
            or prior.get("version") != 1
            or prior.get("unit") != "local-agent-linux.service"
            or prior.get("phase") != "committed"
            or prior.get("candidate_sha") != self.pair.rollback_sha
            or prior.get("candidate_unit_sha256") != expected_digest
        ):
            raise ServiceSwitchError("previous committed upgrade provenance mismatch")
        staged = _read_private_json(self.pair.state / "linux-service-stage-v1.json")
        activated = _read_private_json(
            self.pair.state / "linux-service-activation-v1.json"
        )
        if (
            staged.get("version") != 1
            or staged.get("phase") != "staged"
            or staged.get("unit_name") != "local-agent-linux.service"
            or staged.get("candidate_sha") != prior["baseline_sha"]
            or staged.get("unit_sha256") != prior["baseline_unit_sha256"]
            or activated.get("version") != 1
            or activated.get("phase") != "running"
            or activated.get("unit") != "local-agent-linux.service"
            or activated.get("candidate_sha") != staged["candidate_sha"]
            or activated.get("unit_sha256") != staged["unit_sha256"]
            or activated.get("rollback_sha") != staged.get("rollback_sha")
        ):
            raise ServiceSwitchError("original installation provenance mismatch")

    def _preflight(self) -> None:
        super()._preflight()
        self._original_receipts()


def main(argv: list[str] | None = None) -> int:
    """Reuse v1 CLI parsing and safety logic, selecting only the v2 controller."""
    # Base CLI directly constructs PersistentServiceUpgrade; replace only
    # its lookup in its own module for the duration of this synchronous call.
    # Instead of mutable module patching, keep an explicit independent CLI.
    import argparse
    import json
    from linux_agent.service_upgrade import _external_only
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("inspect", "switch", "rollback", "reconcile"))
    parser.add_argument("--candidate-root", type=Path, required=True)
    parser.add_argument("--candidate-sha", required=True)
    parser.add_argument("--baseline-root", type=Path, required=True)
    parser.add_argument("--baseline-sha", required=True)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--state", type=Path, required=True)
    args = parser.parse_args(argv)
    pair = ReleasePair(
        candidate=args.candidate_root, candidate_sha=args.candidate_sha,
        rollback=args.baseline_root, rollback_sha=args.baseline_sha,
        python=Path(sys.executable).resolve(),
        config=args.config, state=args.state,
    )
    try:
        # Defensive gate also applies to read-only inspect.
        _external_only()
        output: dict[str, Any] = getattr(
            SecondPersistentServiceUpgrade(pair), args.action
        )()
    except (ServiceSwitchError, ValueError, OSError, AssertionError) as exc:
        print("Second Debian upgrade REFUSED: " + str(exc), file=sys.stderr)
        return 2
    print(json.dumps(output, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
